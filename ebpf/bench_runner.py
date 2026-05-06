import json
import subprocess
import os
import signal
import time

TEST_FILE = "./bpf_test_file.bin"
FILE_SIZE_GB = 1
RUNTIME = 5.0


def prepare_file():
    if not os.path.exists(TEST_FILE):
        print(f"[*] Pre-allocating {FILE_SIZE_GB}GB file...")
        subprocess.run(["fallocate", "-l", f"{FILE_SIZE_GB}G", TEST_FILE], check=True)


def get_real_dev_name(dev_id_str):
    try:
        maj_min = dev_id_str.replace("dev(", "").replace(")", "")
        sysfs_path = f"/sys/dev/block/{maj_min}"
        if os.path.exists(sysfs_path):
            return os.path.basename(os.path.realpath(sysfs_path))
    except Exception:
        pass
    return "unknown"


def print_op_stats(op_name, fio_job, bpf_stats):
    # fio 데이터 추출
    fio_cnt = fio_job["total_ios"] if fio_job else 0
    fio_iops = fio_job["iops"] if fio_job else 0
    fio_bytes = fio_job["io_bytes"] if fio_job else 0
    fio_mb = fio_bytes / (1024.0 * 1024.0)  # [추가] 총 데이터 (MB)
    fio_bw = (fio_job["bw_bytes"] / (1024.0 * 1024.0)) if fio_job else 0
    fio_lat = (fio_job["clat_ns"]["mean"] / 1000.0) if (fio_job and fio_cnt > 0) else 0

    # BPF 데이터 추출
    cnt = bpf_stats.get("total_count", 0)
    bpf_bytes = bpf_stats.get("total_bytes", 0)
    bpf_mb = bpf_bytes / (1024.0 * 1024.0)  # [추가] 총 데이터 (MB)
    calc_iops = cnt / RUNTIME
    calc_bw = bpf_mb / RUNTIME
    calc_avg_lat = (bpf_stats.get("total_lat_ns", 0) / cnt / 1000.0) if cnt > 0 else 0
    calc_min_lat = bpf_stats.get("min_lat_ns", 0) / 1000.0 if cnt > 0 else 0
    calc_max_lat = bpf_stats.get("max_lat_ns", 0) / 1000.0 if cnt > 0 else 0

    tag = "[User+Kernel]" if fio_job else "[Kernel Internals]"
    print(f"\n>> {op_name.upper()} {tag}")
    print(f"{'Metric':<15} | {'fio (User Space)':<20} | {'io_trace (Kernel)':<20}")
    print("-" * 65)
    print(f"{'Total IO Cnt':<15} | {fio_cnt:<20} | {cnt:<20}")
    print(
        f"{'Total Data(MB)':<15} | {fio_mb:<20.2f} | {bpf_mb:<20.2f}"
    )  # [추가] 총량 출력
    print(f"{'IOPS':<15} | {fio_iops:<20.0f} | {calc_iops:<20.0f}")
    print(f"{'BW (MB/s)':<15} | {fio_bw:<20.2f} | {calc_bw:<20.2f}")
    print(f"{'Avg Lat (us)':<15} | {fio_lat:<20.2f} | {calc_avg_lat:<20.2f}")
    print(f"{'Min Lat (us)':<15} | {'-':<20} | {calc_min_lat:<20.2f}")
    print(f"{'Max Lat (us)':<15} | {'-':<20} | {calc_max_lat:<20.2f}")

    return fio_cnt, cnt


def run_benchmark():
    prepare_file()

    trace_proc = subprocess.Popen(
        ["sudo", "./io_trace"], stdout=subprocess.PIPE, text=True
    )
    time.sleep(1.5)

    subprocess.run(
        "echo 3 | sudo tee /proc/sys/vm/drop_caches",
        shell=True,
        stdout=subprocess.DEVNULL,
    )

    print("[*] Sending SIGUSR1 to start precise measurement...")
    os.kill(trace_proc.pid, signal.SIGUSR1)
    time.sleep(0.1)

    print(f"[*] Running fio (RandRW 50:50) for {RUNTIME} seconds...")
    fio_cmd = [
        "sudo",
        "fio",
        "--name=nvme_bench",
        f"--filename={TEST_FILE}",
        "--direct=1",
        "--rw=randrw",
        "--rwmixread=50",
        "--bs=4k",
        "--ioengine=libaio",
        "--iodepth=128",
        f"--runtime={int(RUNTIME)}",
        "--time_based",
        "--output-format=json",
    ]
    fio_result = subprocess.run(fio_cmd, capture_output=True, text=True)

    os.kill(trace_proc.pid, signal.SIGINT)
    bpf_output, _ = trace_proc.communicate()

    try:
        fio_data = json.loads(fio_result.stdout)
        job_read = fio_data["jobs"][0]["read"]
        job_write = fio_data["jobs"][0]["write"]
        job_trim = fio_data["jobs"][0].get("trim", None)

        fio_total_ios = job_read["total_ios"] + job_write["total_ios"]

        raw_json = (
            bpf_output.split("---JSON_START---")[1].split("---JSON_END---")[0].strip()
        )
        bpf_data = json.loads(raw_json)

        print(f"\n[Bench Results] Fixed Runtime: {RUNTIME} sec (Granular RW Analysis)")

        for dev in bpf_data["devices"]:
            real_name = get_real_dev_name(dev["dev_name"])
            display_title = f"{dev['dev_name']} [{real_name}]"

            ops = dev["operations"]
            bpf_total_cnt = sum(op["total_count"] for op in ops.values())

            print("\n" + "=" * 65)
            print(f"   PERFORMANCE COMPARISON FOR {display_title}")
            print("=" * 65)

            if bpf_total_cnt > (fio_total_ios * 0.1):  # 타겟 장치
                if "read" in ops or job_read["total_ios"] > 0:
                    print_op_stats("Read (Normal)", job_read, ops.get("read", {}))

                if "write" in ops or job_write["total_ios"] > 0:
                    print_op_stats("Write", job_write, ops.get("write", {}))

                if "read_ahead" in ops:
                    print_op_stats("Read-Ahead", None, ops["read_ahead"])

                if "flush" in ops:
                    print_op_stats("Flush", None, ops["flush"])

                if "discard" in ops:
                    fio_trim_job = (
                        job_trim if (job_trim and job_trim["total_ios"] > 0) else None
                    )
                    print_op_stats("Discard (Trim)", fio_trim_job, ops["discard"])

            else:
                print(
                    f">>> INFO: Background device (Handled {bpf_total_cnt} background IOs)"
                )

    except Exception as e:
        print(f"[-] Parsing Error: {e}")
        print(f"--- RAW BPF OUTPUT ---\n{bpf_output}")


if __name__ == "__main__":
    run_benchmark()
