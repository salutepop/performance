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

def print_op_stats(op_name, fio_job, bpf_stats):
    if bpf_stats.get('total_count', 0) == 0:
        return 0, 0, 0, 0

    cnt = bpf_stats['total_count']
    q2i_ms = bpf_stats.get('q2i', {}).get('total_lat_ns', 0) / 1000000.0
    d2c_ms = bpf_stats.get('d2c', {}).get('total_lat_ns', 0) / 1000000.0

    fio_cnt = fio_job['total_ios'] if fio_job else 0
    
    ebpf_sum_ms = q2i_ms + d2c_ms
    ebpf_avg_us = (ebpf_sum_ms * 1000.0 / cnt) if cnt > 0 else 0

    print(f" [{op_name}] IO Count : fio = {fio_cnt:,} | eBPF = {cnt:,}")
    
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
        
    print(f"  - eBPF HW+OS (Run) : Sum = {ebpf_sum_ms:>10.2f} ms | Avg = {ebpf_avg_us:>8.2f} us (Q2I+D2C)")
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
        "--direct=1", "--rw=randrw", "--rwmixread=50", "--bs=4k", "--ioengine=libaio", 
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
        
        tot_q2i_cnt = tot_q2i_ms = 0
        tot_d2c_cnt = tot_d2c_ms = 0

        for dev in bpf_data['devices']:
            ops = dev['operations']
            bpf_total_cnt = sum(op['total_count'] for op in ops.values())
            
            if bpf_total_cnt > (fio_total_ios * 0.1): # Target Device
                real_name = get_real_dev_name(dev['dev_name'])
                print(f" Target Device: {dev['dev_name']} [{real_name}]\n")
                
                for op_name, fio_src, bpf_src in [
                    ("READ", job_read, ops.get('read', {})),
                    ("WRITE", job_write, ops.get('write', {})),
                    ("READ-AHEAD", None, ops.get('read_ahead', {})),
                    ("FLUSH", None, ops.get('flush', {}))
                ]:
                    if bpf_src:
                        q_cnt, q_ms, d_cnt, d_ms = print_op_stats(op_name, fio_src, bpf_src)
                        tot_q2i_cnt += q_cnt
                        tot_q2i_ms += q_ms
                        tot_d2c_cnt += d_cnt
                        tot_d2c_ms += d_ms
            else:
                print(f" [Background Device: {dev['dev_name']}] Handled {bpf_total_cnt:,} IOs (Skipped)\n")

        sys_stats = bpf_data.get('libaio_overhead', {})
        sub_cnt = sys_stats.get('submit_count', 0)
        sub_sum_ms = sys_stats.get('submit_lat_ns', 0) / 1000000.0
        sub_avg_us = (sys_stats.get('submit_lat_ns', 0) / 1000.0 / sub_cnt) if sub_cnt > 0 else 0
        
        wake_cnt = sys_stats.get('getevents_count', 0)
        wake_sum_ms = sys_stats.get('wakeup_lat_ns', 0) / 1000000.0
        wake_avg_us = (sys_stats.get('wakeup_lat_ns', 0) / 1000.0 / wake_cnt) if wake_cnt > 0 else 0
        
        tot_q2i_avg_us = (tot_q2i_ms * 1000.0 / tot_q2i_cnt) if tot_q2i_cnt > 0 else 0
        tot_d2c_avg_us = (tot_d2c_ms * 1000.0 / tot_d2c_cnt) if tot_d2c_cnt > 0 else 0

        print("-" * 75)
        print(" [ FULL STACK LATENCY BREAKDOWN (Target Dev + Libaio) ]")
        print(f" {'Phase':<15} | {'Call Count':>12} | {'Sum (ms)':>15} | {'Avg (us)':>12}")
        print("-" * 75)
        print(f" {'U2Q (Submit)':<15} | {sub_cnt:>12,} | {sub_sum_ms:>15.2f} | {sub_avg_us:>12.2f}")
        print(f" {'Q2I (OS Queue)':<15} | {tot_q2i_cnt:>12,} | {tot_q2i_ms:>15.2f} | {tot_q2i_avg_us:>12.2f}")
        print(f" {'D2C (Hardware)':<15} | {tot_d2c_cnt:>12,} | {tot_d2c_ms:>15.2f} | {tot_d2c_avg_us:>12.2f}")
        print(f" {'C2U (Wakeup)':<15} | {wake_cnt:>12,} | {wake_sum_ms:>15.2f} | {wake_avg_us:>12.2f}")
        print("=" * 75)

    except Exception as e:
        print(f"[-] Parsing Error: {e}")

if __name__ == "__main__":
    run_benchmark()