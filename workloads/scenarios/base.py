"""
Scenario base — self-checking workload framework. Subclasses override
fio_cmd()/analyze(). Separate from workloads/cases/*.py (the TC framework
driven by `pmon.py monitor --tc`).

Usage:
  class MyScenario(Scenario):
      name = "my_test"
      def fio_cmd(self):
          return "fio --name=my --filename=/tmp/x ... --runtime=10 --time_based ..."
      def analyze(self, summary):
          d2c_p99 = summary["devices"]["nvme0n1"]["ops"]["read"].get("d2c_us_avg")
          return {"d2c_pass": d2c_p99 < 200}

  MyScenario().run()  # -> pmon.py monitor, then analyzes summary.json
"""

import json
import os
import subprocess
import sys

# base.py is workloads/scenarios/base.py -> project root is three levels up.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS_DIR = os.path.join(ROOT, "results")


class Scenario:
    """sub-class override points: name, fio_cmd, optional analyze."""
    name: str = "scenario"
    interval: float = 1.0
    report_formats: str = "all"  # auto report formats (all/md/json/png/pdf/none)

    def fio_cmd(self) -> str:
        """Return the fio command string (fio --name=... --filename=... ...)."""
        raise NotImplementedError("Scenario sub-class must implement fio_cmd()")

    def analyze(self, summary: dict) -> dict:
        """Evaluate the run from summary_<sid>.json. Empty dict = pass.
        A 'pass': bool key in the returned dict drives the exit code."""
        return {}

    # ------------------------------------------------------------------
    # infrastructure (overriding not recommended)
    def run(self) -> int:
        pmon = os.path.join(ROOT, "pmon.py")
        cmd = [sys.executable, pmon, "monitor",
               "--fio", self.fio_cmd(),
               "--label", self.name,
               "--ebpf", "on",
               "--ebpf-interval", str(self.interval),
               "--report", self.report_formats]
        print(f"[scenario {self.name}] launching pmon monitor...")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[scenario {self.name}] pmon exit={rc}", file=sys.stderr)
            return rc
        # Find the newest summary_*.json across all session dirs under results/.
        try:
            from glob import glob
            summaries = glob(os.path.join(RESULTS_DIR, "**", "summary_*.json"),
                             recursive=True)
            if not summaries:
                print(f"[scenario {self.name}] WARN: no summary_*.json — analyze skipped")
                return 0
            summaries.sort(key=os.path.getmtime)
            with open(summaries[-1]) as f:
                summary = json.load(f)
        except Exception as e:
            print(f"[scenario {self.name}] summary load failed: {e}", file=sys.stderr)
            return 1
        result = self.analyze(summary) or {}
        print(f"[scenario {self.name}] analyze ->")
        for k, v in result.items():
            print(f"  {k}: {v}")
        if "pass" in result:
            return 0 if result["pass"] else 1
        return 0
