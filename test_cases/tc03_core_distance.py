import os


class Scenario:
    def __init__(self):
        self.tc_name = "TC03_NUMA_Core_Distance"
        self.description = "시스템의 모든 CPU 코어를 순회하며 지연시간을 측정합니다."

        # 여러 디스크의 결과를 하나로 모으기 위한 인스턴스 변수
        self.all_results = {}

    def execute(self, disk, runner_func, reporter, session_dir, numa_node):
        print(f"\n[Scenario] {self.tc_name} 시작 - 대상: {disk}")
        total_cores = os.cpu_count()

        # 현재 디스크의 결과를 저장할 빈 리스트 생성
        self.all_results[disk] = []

        for core in range(total_cores):
            wl = {
                "name": f"core_{core}_randread",
                "rw": "randread",
                "bs": "4k",
                "iodepth": 1,
                "numjobs": 1,
                "runtime": 1,
                "cpus_allowed": str(core),
                "size": "1G",
            }

            result = runner_func(disk, wl, numa_node=None)

            if result:
                reporter.save_json(session_dir, f"fio_core_{core}.json", result)

                job = result["jobs"][0]
                clat_ns = job["read"]["clat_ns"]["mean"]
                iops = job["read"]["iops"]
                lat_us = clat_ns / 1000

                self.all_results[disk].append(
                    {"core": core, "lat_us": lat_us, "iops": iops}
                )

                # [요청 1] 턴(Turn)마다 결과 즉시 출력
                print(
                    f"  -> [Core {core:2d} 완료] clat: {lat_us:6.2f} us | IOPS: {iops:7.0f}"
                )

        # [요청 2] 현재까지 수집된 모든 디스크의 결과를 하나의 테이블로 병합 출력
        # ram0 측정이 끝나면 ram0 테이블이, ram1까지 끝나면 ram0+ram1 병합 테이블이 출력됩니다.
        self._print_combined_report()

    def _print_combined_report(self):
        print(
            "\n  ======================================================================="
        )
        print("  ▶ [통합 결과] CPU 코어 및 디바이스별 NUMA Distance 요약 리포트")
        print(
            "  ======================================================================="
        )

        disks = list(self.all_results.keys())

        # 디스크 개수에 맞춰 테이블 헤더를 동적으로 생성
        header = f"  {'Core ID':^7} |"
        for d in disks:
            d_name = d.split("/")[-1]
            header += f" {d_name} clat(us) | {d_name} IOPS |"
        print(header)
        print("  " + "-" * (len(header) - 2))

        # 코어 개수만큼 반복하며 행(Row) 생성
        num_cores = len(self.all_results[disks[0]])
        for i in range(num_cores):
            core_id = self.all_results[disks[0]][i]["core"]
            row_str = f"  Core {core_id:2d} |"

            for d in disks:
                if i < len(self.all_results[d]):
                    res = self.all_results[d][i]
                    row_str += f" {res['lat_us']:13.2f} | {res['iops']:9.0f} |"
                else:
                    row_str += "       -       |      -    |"
            print(row_str)

        print(
            "  ======================================================================="
        )
