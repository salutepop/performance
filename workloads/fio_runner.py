import os
import shutil
import subprocess
import json
import sys


def run_fio_job(disk, workload, numa_node=None, fio_path="fio", runtime_override=None):
    """
    fio_path를 인자로 받아 해당 경로의 바이너리를 실행합니다.
    EUID != 0 이면 `sudo -n`을 붙여 호출 (sudoers에 fio NOPASSWD 룰이 있어야 함).
    sudoers 매치를 위해 fio_path는 항상 절대 경로로 resolve.
    """
    job_name = workload.get("name", "default_job")

    # [수정] disk가 리스트인 경우 콜론(:)으로 연결하여 여러 장치 동시 부하 지원
    if isinstance(disk, list):
        target_filename = ":".join(disk)
        print(f"\n[FIO Run] multi-disk ({len(disk)}) | workload: {job_name}...")
    else:
        target_filename = disk
        print(f"\n[FIO Run] disk: {disk} | workload: {job_name}...")

    abs_fio = shutil.which(fio_path) or fio_path
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]

    cmd = prefix + [
        abs_fio,
        f"--name={job_name}",
        f"--filename={target_filename}",
        "--direct=1",
        "--ioengine=libaio",
        f"--rw={workload.get('rw', 'read')}",
        f"--bs={workload.get('bs', '4k')}",
        f"--iodepth={workload.get('iodepth', 1)}",
        f"--numjobs={workload.get('numjobs', 1)}",
        f"--runtime={runtime_override if runtime_override is not None else workload.get('runtime', 3)}",
        "--group_reporting=1",
        "--time_based",
        f"--ramp_time={0 if runtime_override is not None else 3}",  # -q 시에만 0, 나머지는 무조건 3초
        "--output-format=json",
    ]

    if "size" in workload:
        cmd.append(f"--size={workload['size']}")

    if "cpus_allowed" in workload:
        cmd.append(f"--cpus_allowed={workload['cpus_allowed']}")

    if numa_node is not None and str(numa_node).isdigit():
        cmd.append(f"--numa_cpu_nodes={numa_node}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"[Error] {fio_path} execution failed: {e}")
        print(f"[Error Output]\n{e.stderr}")
        return None
    except json.JSONDecodeError:
        print("[Error] cannot parse fio output as JSON")
        return None
    except FileNotFoundError:
        print(f"[Error] fio binary not found: {fio_path}")
        return None
