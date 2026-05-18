"""
Scenario 베이스 — 새 framework. 하위 클래스가 fio_cmd()/analyze()를 override.
기존 test_cases/*.py 와 무관 (deprecated, framework main.py에서 호출).

사용 패턴:
  class MyScenario(Scenario):
      name = "my_test"
      def fio_cmd(self):
          return "fio --name=my --filename=/tmp/x ... --runtime=10 --time_based ..."
      def analyze(self, summary):
          d2c_p99 = summary["devices"]["nvme0n1"]["ops"]["read"].get("d2c_us_avg")
          return {"d2c_pass": d2c_p99 < 200}

  MyScenario().run()  # → pmon.py run 호출, 끝나면 summary.json 분석
"""

import json
import os
import subprocess
import sys
from typing import Optional


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SESSION_DIR = os.path.join(ROOT, "ebpf", "csv_results")


class Scenario:
    """sub-class override 포인트: name, fio_cmd, optional analyze."""
    name: str = "scenario"
    mode: str = "libaio"       # generic / libaio / iouring
    interval: float = 1.0
    report_formats: str = "all"  # 자동 리포트 포맷 (all/html/md/json/none)

    def fio_cmd(self) -> str:
        """fio 명령 문자열 반환. fio --name=... --filename=... 등."""
        raise NotImplementedError("Scenario sub-class must implement fio_cmd()")

    def analyze(self, summary: dict) -> dict:
        """summary_<sid>.json 데이터로 시나리오 평가. 빈 dict면 통과 간주.
        반환 dict에 'pass': bool 키 있으면 종료 코드에 반영."""
        return {}

    # ------------------------------------------------------------------
    # 인프라 (override 비추천)
    def run(self) -> int:
        pmon = os.path.join(ROOT, "pmon.py")
        cmd = [sys.executable, pmon, "run",
               "--fio", self.fio_cmd(),
               "-m", self.mode,
               "-i", str(self.interval),
               "--report", self.report_formats]
        print(f"[scenario {self.name}] launching pmon run...")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[scenario {self.name}] pmon exit={rc}", file=sys.stderr)
            return rc
        # 가장 최근 summary_*.json 찾기
        try:
            from glob import glob
            summaries = sorted(glob(os.path.join(DEFAULT_SESSION_DIR, "summary_*.json")))
            if not summaries:
                print(f"[scenario {self.name}] WARN: summary_*.json 없음 — analyze 스킵")
                return 0
            with open(summaries[-1]) as f:
                summary = json.load(f)
        except Exception as e:
            print(f"[scenario {self.name}] summary 로드 실패: {e}", file=sys.stderr)
            return 1
        result = self.analyze(summary) or {}
        print(f"[scenario {self.name}] analyze →")
        for k, v in result.items():
            print(f"  {k}: {v}")
        if "pass" in result:
            return 0 if result["pass"] else 1
        return 0
