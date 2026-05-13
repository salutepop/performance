import json
import subprocess
import os
import signal
import time
import argparse


def get_real_dev_name(dev_id_str):
    try:
        maj_min = dev_id_str.replace("dev(", "").replace(")", "")
        sysfs_path = f"/sys/dev/block/{maj_min}"
        if os.path.exists(sysfs_path):
            return os.path.basename(os.path.realpath(sysfs_path))
    except Exception:
        pass
    return "unknown"


def print_op_stats(op_name, bpf_stats, c2a_data, a2u_data, duration):
    c2a_cnt, c2a_ms = c2a_data if c2a_data else (0, 0)
    a2u_cnt, a2u_ms = a2u_data if a2u_data else (0, 0)

    if bpf_stats.get("total_count", 0) == 0:
        return 0, 0, 0, 0, 0

    cnt = bpf_stats["total_count"]
    bpf_bytes = bpf_stats.get("total_bytes", 0)
    bpf_bw_mb = (bpf_bytes / (1024.0 * 1024.0)) / duration if duration > 0 else 0

    q2d_ms = bpf_stats.get("q2d", {}).get("total_lat_ns", 0) / 1000000.0
    d2c_ms = bpf_stats.get("d2c", {}).get("total_lat_ns", 0) / 1000000.0

    q2d_avg_us = (q2d_ms * 1000.0 / cnt) if cnt > 0 else 0
    d2c_avg_us = (d2c_ms * 1000.0 / cnt) if cnt > 0 else 0
    c2a_avg_us = (c2a_ms * 1000.0 / c2a_cnt) if c2a_cnt > 0 else 0
    a2u_avg_us = (a2u_ms * 1000.0 / a2u_cnt) if a2u_cnt > 0 else 0

    ebpf_sum_ms = q2d_ms + d2c_ms + c2a_ms + a2u_ms
    ebpf_avg_us = q2d_avg_us + d2c_avg_us + c2a_avg_us + a2u_avg_us

    print(f" [{op_name}] IO Count : {cnt:,} (C2A={c2a_cnt:,}, A2U={a2u_cnt:,})")
    print(f"  - Total Bytes : {bpf_bytes:,} B")
    print(f"  - Bandwidth   : {bpf_bw_mb:>10.2f} MB/s")
    print(
        f"  - Full Stack (Run) : Sum = {ebpf_sum_ms:>10.2f} ms | Avg = {ebpf_avg_us:>8.2f} us (Q2D+D2C+C2A+A2U)"
    )

    hist = bpf_stats.get("size_hist", [0, 0, 0, 0])
    if cnt > 0 and sum(hist) > 0:
        labels = ["<= 4KB", "4K-32K", "32K-128K", "> 128KB"]
        print(f"  - IO Size Dist :")
        for i in range(4):
            count = hist[i]
            ratio = (count / cnt) * 100
            bar = "█" * int(ratio / 5)
            print(f"      {labels[i]:>10} : [{bar:<20}] {ratio:>5.1f}% ({count:,})")

    print()

    return cnt, q2d_ms, d2c_ms, c2a_cnt, c2a_ms


def run_benchmark(mode="generic", cmd=None, script_file=None):
    # 1. 모니터링 시작 (eBPF Tracer 구동)
    trace_cmd = ["sudo", "./io_trace"]
    if mode != "generic":
        trace_cmd.extend(["-m", mode])
        print(f"[*] eBPF Tracer starting in: {mode.upper()} Mode")
    else:
        print("[*] eBPF Tracer starting in: Generic Block Mode (Default)")

    trace_proc = subprocess.Popen(trace_cmd, stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)  # eBPF 맵 로드 대기

    # 캐시 비우기
    subprocess.run(
        "echo 3 | sudo tee /proc/sys/vm/drop_caches",
        shell=True,
        stdout=subprocess.DEVNULL,
    )
    os.kill(trace_proc.pid, signal.SIGUSR1)  # 맵 초기화 시그널 전송
    time.sleep(0.1)

    t0 = time.time()

    # 2. 평가 시작
    try:
        if cmd:
            print(f"[*] Executing custom command: {cmd}\n")
            # 쉘 환경에서 직접 문자열 명령어 실행
            subprocess.run(cmd, shell=True)

        elif script_file:
            print(f"[*] Executing script file: {script_file}\n")
            # 스크립트 파일 실행 (bash 환경)
            subprocess.run(f"bash {script_file}", shell=True)

        else:
            print("[*] No workload command provided.")
            print("[*] Monitoring in background... (Press Ctrl+C to stop)\n")
            # 명령어가 없으면 백그라운드 무한 대기
            while True:
                time.sleep(1)

    except KeyboardInterrupt:
        # 3. 강제 중단 시 안전하게 캐치
        print("\n[*] Workload or monitoring forcefully stopped by user.")
    except Exception as e:
        print(f"\n[-] Error running workload: {e}")

    # 4. 모니터링 종료 및 데이터 파싱
    t1 = time.time()
    effective_duration = t1 - t0

    print("\n[*] Stopping eBPF tracer and collecting data...")
    os.kill(trace_proc.pid, signal.SIGINT)
    bpf_output, _ = trace_proc.communicate()

    if effective_duration <= 0:
        effective_duration = 1.0  # Divide by zero 방지

    try:
        raw_json = (
            bpf_output.split("---JSON_START---")[1].split("---JSON_END---")[0].strip()
        )
        bpf_data = json.loads(raw_json)

        print("=" * 100)
        print(f" [ I/O PROFILING REPORT | Duration: {effective_duration:.2f} seconds ]")
        print("=" * 100)

        sys_stats = bpf_data.get("libaio_overhead", {})
        c2a_read = (
            sys_stats.get("c2a_read_count", 0),
            sys_stats.get("c2a_read_total", 0) / 1000000.0,
        )
        c2a_write = (
            sys_stats.get("c2a_write_count", 0),
            sys_stats.get("c2a_write_total", 0) / 1000000.0,
        )
        c2a_flush = (
            sys_stats.get("c2a_flush_count", 0),
            sys_stats.get("c2a_flush_total", 0) / 1000000.0,
        )

        a2u_read = (
            sys_stats.get("a2u_read_count", 0),
            sys_stats.get("a2u_read_total", 0) / 1000000.0,
        )
        a2u_write = (
            sys_stats.get("a2u_write_count", 0),
            sys_stats.get("a2u_write_total", 0) / 1000000.0,
        )
        a2u_flush = (
            sys_stats.get("a2u_flush_count", 0),
            sys_stats.get("a2u_flush_total", 0) / 1000000.0,
        )

        phase_stats = {"U2Q": {}, "Q2D": {}, "D2C": {}, "C2A": {}, "A2U": {}}
        tot_q2d_cnt = tot_q2d_ms = tot_d2c_ms = 0

        # 전체 IO 수 파악
        total_sys_ios = 0
        for dev in bpf_data["devices"]:
            total_sys_ios += sum(op["total_count"] for op in dev["operations"].values())

        for dev in bpf_data["devices"]:
            ops = dev["operations"]
            bpf_total_cnt = sum(op["total_count"] for op in ops.values())

            # 노이즈 필터링 (5% 이상)
            if bpf_total_cnt > 0 and bpf_total_cnt > (total_sys_ios * 0.05):
                real_name = get_real_dev_name(dev["dev_name"])
                print(f" Target Device: {dev['dev_name']} [{real_name}]\n")

                for op_name, bpf_src, c2a_data, a2u_data in [
                    ("READ", ops.get("read", {}), c2a_read, a2u_read),
                    ("WRITE", ops.get("write", {}), c2a_write, a2u_write),
                    ("READ-AHEAD", ops.get("read_ahead", {}), None, None),
                    ("FLUSH", ops.get("flush", {}), c2a_flush, a2u_flush),
                ]:
                    if bpf_src:
                        q_cnt, q_ms, d_ms, c_cnt, c_ms = print_op_stats(
                            op_name, bpf_src, c2a_data, a2u_data, effective_duration
                        )

                        tot_q2d_cnt += q_cnt
                        tot_q2d_ms += q_ms
                        tot_d2c_ms += d_ms

                        phase_stats["Q2D"][op_name] = (
                            q_cnt,
                            q_ms,
                            (q_ms * 1000.0 / q_cnt) if q_cnt > 0 else 0,
                        )
                        phase_stats["D2C"][op_name] = (
                            q_cnt,
                            d_ms,
                            (d_ms * 1000.0 / q_cnt) if q_cnt > 0 else 0,
                        )

                        if c2a_data:
                            phase_stats["C2A"][op_name] = (
                                c_cnt,
                                c_ms,
                                (c_ms * 1000.0 / c_cnt) if c_cnt > 0 else 0,
                            )
                        else:
                            phase_stats["C2A"][op_name] = (0, 0, 0)

                        if a2u_data:
                            a_cnt, a_ms = a2u_data
                            phase_stats["A2U"][op_name] = (
                                a_cnt,
                                a_ms,
                                (a_ms * 1000.0 / a_cnt) if a_cnt > 0 else 0,
                            )
                        else:
                            phase_stats["A2U"][op_name] = (0, 0, 0)
            else:
                if bpf_total_cnt > 0:
                    print(
                        f" [Background Device: {dev['dev_name']}] Handled {bpf_total_cnt:,} IOs (Skipped)\n"
                    )

        u2q_cnt = sys_stats.get("u2q_count", 0)
        u2q_sum_ms = sys_stats.get("u2q_lat_total", 0) / 1000000.0
        u2q_avg_us = (
            (sys_stats.get("u2q_lat_total", 0) / 1000.0 / u2q_cnt) if u2q_cnt > 0 else 0
        )
        phase_stats["U2Q"]["Total"] = (u2q_cnt, u2q_sum_ms, u2q_avg_us)

        tot_c2a_cnt = c2a_read[0] + c2a_write[0] + c2a_flush[0]
        tot_c2a_ms = c2a_read[1] + c2a_write[1] + c2a_flush[1]
        tot_c2a_avg = (tot_c2a_ms * 1000.0 / tot_c2a_cnt) if tot_c2a_cnt > 0 else 0
        phase_stats["C2A"]["Total"] = (tot_c2a_cnt, tot_c2a_ms, tot_c2a_avg)

        tot_a2u_cnt = a2u_read[0] + a2u_write[0] + a2u_flush[0]
        tot_a2u_ms = a2u_read[1] + a2u_write[1] + a2u_flush[1]
        tot_a2u_avg = (tot_a2u_ms * 1000.0 / tot_a2u_cnt) if tot_a2u_cnt > 0 else 0
        phase_stats["A2U"]["Total"] = (tot_a2u_cnt, tot_a2u_ms, tot_a2u_avg)

        tot_q2d_avg_us = (tot_q2d_ms * 1000.0 / tot_q2d_cnt) if tot_q2d_cnt > 0 else 0
        tot_d2c_avg_us = (tot_d2c_ms * 1000.0 / tot_q2d_cnt) if tot_q2d_cnt > 0 else 0

        phase_stats["Q2D"]["Total"] = (tot_q2d_cnt, tot_q2d_ms, tot_q2d_avg_us)
        phase_stats["D2C"]["Total"] = (tot_q2d_cnt, tot_d2c_ms, tot_d2c_avg_us)

        table_width = 100
        print("-" * table_width)
        print(f" {'[ FULL STACK LATENCY BREAKDOWN ]':^{table_width - 2}}")
        print("-" * table_width)
        print(
            f" {'Phase':<18} | {'Metric':<10} | {'Total':>12} | {'READ':>12} | {'WRITE':>12} | {'READ-AHEAD':>10} | {'FLUSH':>8}"
        )
        print("-" * table_width)

        def print_phase(phase_name, phase_key):
            stats = phase_stats.get(phase_key, {})

            def get_val(op, idx):
                if op not in stats or stats[op][0] == 0:
                    return "-"
                val = stats[op][idx]
                if idx == 0:
                    return f"{int(val):,}"
                else:
                    return f"{val:.2f}"

            def format_row(phase_name_str, metric_name, idx):
                tot = get_val("Total", idx)
                r = get_val("READ", idx)
                w = get_val("WRITE", idx)
                ra = get_val("READ-AHEAD", idx)
                fl = get_val("FLUSH", idx)

                if phase_key == "U2Q":
                    r = w = ra = fl = "-"
                if phase_key in ["C2A", "A2U"]:
                    ra = "-"

                print(
                    f" {phase_name_str:<18} | {metric_name:<10} | {tot:>12} | {r:>12} | {w:>12} | {ra:>10} | {fl:>8}"
                )

            format_row(phase_name, "Call Count", 0)
            format_row("", "Sum (ms)", 1)
            format_row("", "Avg (us)", 2)
            print("-" * table_width)

        print_phase("U2Q (User->BLK_Q)", "U2Q")
        print_phase("Q2D (BLK_Q->Disp)", "Q2D")
        print_phase("D2C (Disp->Compl)", "D2C")
        if mode != "generic":
            print_phase("C2A (Compl->AIO)", "C2A")
            print_phase("A2U (AIO->User)", "A2U")
        print("=" * table_width)

    except Exception as e:
        print(f"[-] Parsing Error: {e}")
        print(f"Raw Output Snippet:\n{bpf_output[:500]}...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Universal eBPF I/O Monitor & Profiler"
    )

    # 1. 모드 선택 (기존과 동일)
    parser.add_argument(
        "-m",
        "--mode",
        type=str,
        default="generic",
        choices=["generic", "libaio", "iouring"],
        help="Select tracing mode: generic (default), libaio, iouring",
    )

    # 2. 명령어 또는 파일 입력 (둘 중 하나만 사용 가능하도록 제한)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-c",
        "--cmd",
        type=str,
        help="Command string to execute (e.g., -c 'fio --name=test --size=1G')",
    )
    group.add_argument(
        "-f",
        "--file",
        type=str,
        help="Shell script file to execute (e.g., -f ./run_workload.sh)",
    )

    args = parser.parse_args()

    run_benchmark(mode=args.mode, cmd=args.cmd, script_file=args.file)
