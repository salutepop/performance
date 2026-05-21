"""scenario: GC stress + p99 collection.

흐름:
1. precondition phase: 1MB seq write로 디스크 채움 (flash GC 시드)
2. measure phase: 4K randwrite mixed로 GC/SLC 캐시 효과 유도, p99 tail 변화 관찰

주: 임시 파일(/tmp/fio_smoke.dat) 대상이라 ext4 layer + 실제 flash GC 일부만 노출.
raw NVMe partition으로 바꾸면 더 직접적이지만 destructive하므로 기본 안전 경로 유지.

p99 변화는 PNG report (figs_<sid>/*_lat.png)의 d2c p99 차트로 시각화됨.

실행:
  python3 -m workloads.scenarios.gc_stress
"""

import sys

from .base import Scenario


class GcStress(Scenario):
    name = "gc_stress"
    interval = 1.0

    def fio_cmd(self):
        # --stonewall로 두 job을 sequential 실행. eBPF는 둘 다 같은 트레이스 세션에서 캡처.
        # precondition은 짧게 (smoke 환경 고려), 실제 GC 유도는 더 큰 size + 더 긴 runtime 필요.
        return (
            "fio "
            "--name=precond --filename=/tmp/fio_smoke.dat --rw=write --bs=1M "
            "--iodepth=16 --size=128M --direct=1 --ioengine=libaio --numjobs=1 "
            "--stonewall "
            "--name=gc_measure --filename=/tmp/fio_smoke.dat --rw=randwrite --bs=4k "
            "--iodepth=32 --size=128M --runtime=6 --time_based --direct=1 "
            "--ioengine=libaio --numjobs=2 --group_reporting"
        )

    def analyze(self, summary):
        devs = summary.get("devices") or {}
        if not devs:
            return {"pass": False, "reason": "no devices"}
        dname = next(iter(devs))
        write = devs[dname]["ops"].get("write") or {}
        total_io = write.get("total_io") or 0
        d2c_avg = write.get("d2c_us_avg")
        # 두 phase의 통합이라 d2c_avg가 큰 outlier 영향 받을 수 있음 — 1ms 이상이면 GC 영향 가능
        gc_likely = d2c_avg is not None and d2c_avg > 1000
        return {
            "device": dname,
            "write_total_io": total_io,
            "write_d2c_avg_us": d2c_avg,
            "gc_signature": gc_likely,
            "see_p99_chart": "figs_<sid>/*_lat.png → D2C p99 차트",
            "pass": total_io > 1000,
        }


if __name__ == "__main__":
    sys.exit(GcStress().run())
