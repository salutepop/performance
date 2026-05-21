"""예시 scenario — 4K randread 5초, d2c p99 < 1ms 검증.

실행:
  python3 -m workloads.scenarios.sample_randread
"""

import sys

from .base import Scenario


class SampleRandRead(Scenario):
    name = "sample_randread"
    interval = 1.0

    def fio_cmd(self):
        return (
            "fio --name=sample_randread --filename=/tmp/fio_smoke.dat "
            "--rw=randread --bs=4k --iodepth=8 --size=64M --runtime=5 "
            "--time_based --direct=1 --ioengine=libaio --numjobs=1 "
            "--group_reporting"
        )

    def analyze(self, summary):
        # 첫 NVMe device의 read 통계 사용
        devs = summary.get("devices", {})
        if not devs:
            return {"pass": False, "reason": "no devices in summary"}
        dname = next(iter(devs))
        read = devs[dname]["ops"].get("read") or {}
        d2c_avg = read.get("d2c_us_avg")
        iops = read.get("total_io")
        pass_d2c = d2c_avg is not None and d2c_avg < 1000  # 1ms
        return {
            "device": dname,
            "total_io": iops,
            "d2c_avg_us": d2c_avg,
            "d2c_under_1ms": pass_d2c,
            "pass": pass_d2c and (iops or 0) > 1000,
        }


if __name__ == "__main__":
    sys.exit(SampleRandRead().run())
