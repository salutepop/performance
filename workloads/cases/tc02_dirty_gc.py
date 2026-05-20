import time
import subprocess


class Scenario:
    def __init__(self):
        self.tc_name = "TC02_Dirty_State_GC"
        self.description = (
            "디스크에 쓰기 부하를 가한 후 캐시를 비우고 극한의 지연시간을 측정합니다."
        )

    def execute(self, disk, runner_func, reporter, session_dir, numa_node, sys_info=None):
        print(f"\n[Scenario] {self.tc_name} 시작 - 대상: {disk}")

        # Step 1: Preconditioning (디스크 부하 주기)
        print("  -> [Step 1] Preconditioning 시작 (1M 순차 쓰기)...")
        precond_wl = {"name": "precond_write", "rw": "write", "bs": "1M", "iodepth": 32}

        # runner_func를 호출하면 프레임워크가 알아서 fio를 실행하고 결과를 돌려줍니다.
        runner_func(disk, precond_wl, numa_node)

        # Step 2: OS 캐시 Drop 및 GC 대기
        print("  -> [Step 2] OS 캐시 비우기 및 안정화 대기 (3초)...")
        subprocess.run("sync; echo 3 > /proc/sys/vm/drop_caches", shell=True)
        time.sleep(3)  # 실제 SSD 평가 시에는 펌웨어 GC 시간을 고려해 더 길게 줍니다.

        # Step 3: Dirty State에서의 Random Read Latency 측정
        print("  -> [Step 3] 안정화 상태에서 4K 랜덤 읽기 측정 (QD=1)...")
        lat_wl = {"name": "dirty_randread", "rw": "randread", "bs": "4k", "iodepth": 1}
        result = runner_func(disk, lat_wl, numa_node)

        # Step 4: 결과 저장 및 조건부 동작
        if result:
            reporter.save_json(session_dir, "fio_dirty_randread.json", result)
            reporter.print_summary("dirty_randread", result)

            # 파이썬이므로 측정된 결과를 바탕으로 즉각적인 조건부 대응이 가능합니다.
            lat_ns = result["jobs"][0]["read"]["lat_ns"]["mean"]
            lat_us = lat_ns / 1000

            if lat_us > 50.0:
                print(
                    f"  -> [Warning] 지연시간이 50us를 초과했습니다! ({lat_us:.2f} us)"
                )
                print(
                    "  -> [Step 5] 원인 분석을 위해 큐 뎁스를 늘려 추가 테스트를 진행합니다."
                )
                debug_wl = {
                    "name": "debug_high_qd",
                    "rw": "randread",
                    "bs": "4k",
                    "iodepth": 256,
                }
                debug_res = runner_func(disk, debug_wl, numa_node)
                reporter.save_json(session_dir, "fio_debug_high_qd.json", debug_res)
