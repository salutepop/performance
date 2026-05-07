import json
import subprocess
import os
import signal
import time

TEST_FILE = "./bpf_test_file.bin"
FILE_SIZE_GB = 1
RUNTIME = 5.0 

def get_real_dev_name(dev_id_str):
    try:
        maj_min = dev_id_str.replace("dev(", "").replace(")", "")
        sysfs_path = f"/sys/dev/block/{maj_min}"
        if os.path.exists(sysfs_path):
            return os.path.basename(os.path.realpath(sysfs_path))
    except Exception:
        pass
    return "unknown"

def print_op_stats(op_name, fio_job, bpf_stats, c2u_data):
    c2u_cnt, c2u_ms = c2u_data
    if bpf_stats.get('total_count', 0) == 0:
        return 0, 0, 0, 0

    cnt = bpf_stats['total_count']
    q2i_ms = bpf_stats.get('q2i', {}).get('total_lat_ns', 0) / 1000000.0
    d2c_ms = bpf_stats.get('d2c', {}).get('total_lat_ns', 0) / 1000000.0

    fio_cnt = fio_job['total_ios'] if fio_job else 0
    
    q2i_avg_us = (q2i_ms * 1000.0 / cnt) if cnt > 0 else 0
    d2c_avg_us = (d2c_ms * 1000.0 / cnt) if cnt > 0 else 0
    c2u_avg_us = (c2u_ms * 1000.0 / c2u_cnt) if c2u_cnt > 0 else 0

    ebpf_sum_ms = q2i_ms + d2c_ms + c2u_ms
    ebpf_avg_us = q2i_avg_us + d2c_avg_us + c2u_avg_us

    print(f" [{op_name}] IO Count : fio = {fio_cnt:,} | eBPF = {cnt:,} (C2U={c2u_cnt:,})")
    
    if fio_job and fio_cnt > 0:
        lat_ns = fio_job.get('lat_ns', {}).get('mean', 0)
        slat_ns = fio_job.get('slat_ns', {}).get('mean', 0)
        clat_ns = fio_job.get('clat_ns', {}).get('mean', 0)

        lat_avg_us = lat_ns / 1000.0
        slat_avg_us = slat_ns / 1000.0
        clat_avg_us = clat_ns / 1000.0

        lat_sum_ms = (lat_ns * fio_cnt) / 1000000.0
        slat_sum_ms = (slat_ns * fio_cnt) / 1000000.0
        clat_sum_ms = (clat_ns * fio_cnt) / 1000000.0

        print(f"  - fio  lat  (Total): Sum = {lat_sum_ms:>10.2f} ms | Avg = {lat_avg_us:>8.2f} us")
        print(f"  - fio  slat (Wait) : Sum = {slat_sum_ms:>10.2f} ms | Avg = {slat_avg_us:>8.2f} us")
        print(f"  - fio  clat (Run)  : Sum = {clat_sum_ms:>10.2f} ms | Avg = {clat_avg_us:>8.2f} us")
        
    print(f"  - eBPF HW+OS (Run) : Sum = {ebpf_sum_ms:>10.2f} ms | Avg = {ebpf_avg_us:>8.2f} us (Q2I+D2C+C2U)")
    print()

    return cnt, q2i_ms, cnt, d2c_ms

def run_benchmark():
    trace_proc = subprocess.Popen(["sudo", "./io_trace"], stdout=subprocess.PIPE, text=True)
    time.sleep(1.5) 

    subprocess.run("echo 3 | sudo tee /proc/sys/vm/drop_caches", shell=True, stdout=subprocess.DEVNULL)
    os.kill(trace_proc.pid, signal.SIGUSR1)
    time.sleep(0.1)

    print(f"[*] Running fio (RandRW 50:50) for {RUNTIME} seconds...\n")
    
    fio_cmd = [
        "sudo", "fio", "--name=nvme_bench", f"--filename={TEST_FILE}", f"--size={FILE_SIZE_GB}G",   
        "--direct=1", "--rw=randrw", "--rwmixread=0", "--bs=4k", "--ioengine=libaio", 
        "--iodepth=128", f"--runtime={int(RUNTIME)}", 
        "--time_based", "--output-format=json"
    ]
    fio_result = subprocess.run(fio_cmd, capture_output=True, text=True)

    os.kill(trace_proc.pid, signal.SIGINT)
    bpf_output, _ = trace_proc.communicate()

    try:
        fio_data = json.loads(fio_result.stdout)
        job_read = fio_data['jobs'][0]['read']
        job_write = fio_data['jobs'][0]['write']
        job_trim = fio_data['jobs'][0].get('trim', None)
        fio_total_ios = job_read['total_ios'] + job_write['total_ios']

        raw_json = bpf_output.split("---JSON_START---")[1].split("---JSON_END---")[0].strip()
        bpf_data = json.loads(raw_json)

        print("="*75)
        print(" [ I/O PROFILING REPORT (with slat/clat breakdown) ]")
        print("="*75)
        
        sys_stats = bpf_data.get('libaio_overhead', {})
        c2u_read = (sys_stats.get('c2u_read_count', 0), sys_stats.get('c2u_read_total', 0) / 1000000.0)
        c2u_write = (sys_stats.get('c2u_write_count', 0), sys_stats.get('c2u_write_total', 0) / 1000000.0)
        c2u_flush = (sys_stats.get('c2u_flush_count', 0), sys_stats.get('c2u_flush_total', 0) / 1000000.0)

        # 테이블 렌더링을 위해 데이터를 수집할 딕셔너리
        phase_stats = {'U2Q': {}, 'Q2I': {}, 'D2C': {}, 'C2U': {}}
        tot_q2i_cnt = tot_q2i_ms = 0
        tot_d2c_cnt = tot_d2c_ms = 0

        for dev in bpf_data['devices']:
            ops = dev['operations']
            bpf_total_cnt = sum(op['total_count'] for op in ops.values())
            
            if bpf_total_cnt > (fio_total_ios * 0.1): # Target Device
                real_name = get_real_dev_name(dev['dev_name'])
                print(f" Target Device: {dev['dev_name']} [{real_name}]\n")
                
                for op_name, fio_src, bpf_src, c2u_data in [
                    ("READ", job_read, ops.get('read', {}), c2u_read),
                    ("WRITE", job_write, ops.get('write', {}), c2u_write),
                    ("READ-AHEAD", None, ops.get('read_ahead', {}), None),
                    ("FLUSH", None, ops.get('flush', {}), c2u_flush)
                ]:
                    if bpf_src:
                        q_cnt, q_ms, d_cnt, d_ms = print_op_stats(op_name, fio_src, bpf_src, c2u_data if c2u_data else (0,0))
                        tot_q2i_cnt += q_cnt
                        tot_q2i_ms += q_ms
                        tot_d2c_cnt += d_cnt
                        tot_d2c_ms += d_ms

                        q2i_avg = (q_ms * 1000.0 / q_cnt) if q_cnt > 0 else 0
                        d2c_avg = (d_ms * 1000.0 / d_cnt) if d_cnt > 0 else 0
                        phase_stats['Q2I'][op_name] = (q_cnt, q_ms, q2i_avg)
                        phase_stats['D2C'][op_name] = (d_cnt, d_ms, d2c_avg)

                        if c2u_data:
                            c2u_cnt, c2u_ms = c2u_data
                            c2u_avg = (c2u_ms * 1000.0 / c2u_cnt) if c2u_cnt > 0 else 0
                            phase_stats['C2U'][op_name] = (c2u_cnt, c2u_ms, c2u_avg)
                        else:
                            phase_stats['C2U'][op_name] = (0, 0, 0)
            else:
                print(f" [Background Device: {dev['dev_name']}] Handled {bpf_total_cnt:,} IOs (Skipped)\n")

        # Global 통계 계산
        sub_cnt = sys_stats.get('submit_count', 0)
        sub_sum_ms = sys_stats.get('submit_lat_ns', 0) / 1000000.0
        sub_avg_us = (sys_stats.get('submit_lat_ns', 0) / 1000.0 / sub_cnt) if sub_cnt > 0 else 0
        phase_stats['U2Q']['Total'] = (sub_cnt, sub_sum_ms, sub_avg_us)

        wake_cnt = sys_stats.get('getevents_count', 0)
        wake_sum_ms = sys_stats.get('wakeup_lat_ns', 0) / 1000000.0
        wake_avg_us = (sys_stats.get('wakeup_lat_ns', 0) / 1000.0 / wake_cnt) if wake_cnt > 0 else 0

        tot_q2i_avg_us = (tot_q2i_ms * 1000.0 / tot_q2i_cnt) if tot_q2i_cnt > 0 else 0
        tot_d2c_avg_us = (tot_d2c_ms * 1000.0 / tot_d2c_cnt) if tot_d2c_cnt > 0 else 0

        phase_stats['Q2I']['Total'] = (tot_q2i_cnt, tot_q2i_ms, tot_q2i_avg_us)
        phase_stats['D2C']['Total'] = (tot_d2c_cnt, tot_d2c_ms, tot_d2c_avg_us)
        phase_stats['C2U']['Total'] = (wake_cnt, wake_sum_ms, wake_avg_us)

        # ---------------------------------------------------------
        # 매트릭스 스타일 통합 출력
        # ---------------------------------------------------------
        table_width = 100
        print("-" * table_width)
        print(f" {'[ FULL STACK LATENCY BREAKDOWN (Target Dev + Libaio) ]':^{table_width-2}}")
        print("-" * table_width)
        print(f" {'Phase':<15} | {'Metric':<10} | {'Total':>12} | {'READ':>12} | {'WRITE':>12} | {'READ-AHEAD':>10} | {'FLUSH':>8}")
        print("-" * table_width)

        def print_phase(phase_name, phase_key):
            stats = phase_stats.get(phase_key, {})

            def get_val(op, idx):
                if op not in stats or stats[op][0] == 0: return "-"
                val = stats[op][idx]
                if idx == 0: return f"{int(val):,}"
                else: return f"{val:.2f}"

            def format_row(phase_name_str, metric_name, idx):
                tot = get_val('Total', idx)
                r = get_val('READ', idx)
                w = get_val('WRITE', idx)
                ra = get_val('READ-AHEAD', idx)
                fl = get_val('FLUSH', idx)
                
                # U2Q는 명령어 분기가 불가능하므로 빈칸 처리
                if phase_key == 'U2Q': r = w = ra = fl = "-"
                # READ-AHEAD는 백그라운드이므로 C2U 유저스페이스 웩업이 없음
                if phase_key == 'C2U': ra = "-"
                    
                print(f" {phase_name_str:<15} | {metric_name:<10} | {tot:>12} | {r:>12} | {w:>12} | {ra:>10} | {fl:>8}")

            format_row(phase_name, 'Call Count', 0)
            format_row('', 'Sum (ms)', 1)
            format_row('', 'Avg (us)', 2)
            print("-" * table_width)

        print_phase('U2Q (Submit)', 'U2Q')
        print_phase('Q2I (OS Queue)', 'Q2I')
        print_phase('D2C (Hardware)', 'D2C')
        print_phase('C2U (Wakeup)', 'C2U')
        print("=" * table_width)

    except Exception as e:
        print(f"[-] Parsing Error: {e}")

if __name__ == "__main__":
    run_benchmark()