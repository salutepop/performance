# core/reporter.py
import os
import json
import datetime


class ResultReporter:
    def __init__(self, base_dir="results", run_label=None):
        self.base_dir = base_dir
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"run_{timestamp}" if not run_label else f"run_{timestamp}_{run_label}"
        self.run_dir = os.path.join(self.base_dir, run_name)
        os.makedirs(self.run_dir, exist_ok=True)

    def create_session_dir(self, tc_name, disk_label):
        sub_name = f"{tc_name}_{disk_label}"
        dir_name = os.path.join(self.run_dir, sub_name)
        # 같은 평가 내에서 동일 (tc, disk) 조합이 두 번 호출되는 드문 경우 suffix 부여
        if os.path.exists(dir_name):
            i = 2
            while os.path.exists(f"{dir_name}_{i}"):
                i += 1
            dir_name = f"{dir_name}_{i}"
        os.makedirs(dir_name, exist_ok=True)
        return dir_name

    def save_json(self, target_dir, filename, data):
        filepath = os.path.join(target_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    def print_summary(self, wl_name, result_data):
        if not result_data or "jobs" not in result_data:
            return

        job = result_data["jobs"][0]
        mode = "read" if job["read"]["bw"] > 0 else "write"

        bw_mb = job[mode]["bw"] / 1024
        iops = job[mode]["iops"]
        lat_us = job[mode]["clat_ns"]["mean"] / 1000

        print(
            f"  >> [{wl_name} 완료] BW: {bw_mb:.2f} MB/s | IOPS: {iops:.0f} | Latency: {lat_us:.2f} us"
        )
