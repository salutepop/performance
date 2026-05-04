# core/monitor.py
import subprocess
import os


class SystemMonitor:
    def __init__(self, session_dir):
        self.session_dir = session_dir
        self.iostat_proc = None
        self.mpstat_proc = None

    def start(self):
        print("  -> [Monitor] 1초 단위 iostat / mpstat 수집 시작...")
        iostat_log = os.path.join(self.session_dir, "iostat.log")
        mpstat_log = os.path.join(self.session_dir, "mpstat.log")

        # 터미널 스크립트 실행 (1초 단위 백그라운드)
        self.iostat_proc = subprocess.Popen(
            ["bash", "scripts/collect_iostat.sh", iostat_log]
        )
        self.mpstat_proc = subprocess.Popen(
            ["bash", "scripts/collect_mpstat.sh", mpstat_log]
        )

    def stop(self):
        if self.iostat_proc:
            self.iostat_proc.terminate()
        if self.mpstat_proc:
            self.mpstat_proc.terminate()
        print("  -> [Monitor] 모니터링 수집 종료.")
