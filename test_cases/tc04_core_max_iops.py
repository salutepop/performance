import os


class Scenario:
    def __init__(self):
        self.tc_name = "TC04_NUMA_Core_Max_Perf"
        self.description = "시스템의 모든 CPU 코어를 순회하며 QD64 환경에서 단일 코어 최대 IOPS와 지연시간을 측정합니다."
        # 여러 디스크의 결과를 하나로 모으기 위한 인스턴스 변수
        self.all_results = {}

    def execute(self, disk, runner_func, reporter, session_dir, numa_node, sys_info=None):
        print(f"\n[Scenario] {self.tc_name} 시작 - 대상: {disk}")
        total_cores = os.cpu_count()

        # 현재 디스크의 결과를 저장할 빈 리스트 생성
        self.all_results[disk] = []

        for core in range(total_cores):
            # [수정] 단일 코어 최대 성능 측정을 위해 QD64 설정
            wl = {
                "name": f"core_{core}_qd64_randread",
                "rw": "randread",
                "bs": "4k",
                "iodepth": 64,  # 단일 코어 최대 성능을 뽑기 위한 QD 설정
                "numjobs": 1,  # 1개 Job으로 한정하여 코어 성능 집중
                "runtime": 10,  # 안정적인 측정을 위해 10초로 증설 (기존 1초는 너무 짧음)
                "time_based": 1,
                "cpus_allowed": str(core),
                "direct": 1,  # Buffer I/O 배제
                "group_reporting": 1,
            }

            # runner_func 실행
            result = runner_func(disk, wl, numa_node=None)

            if result:
                reporter.save_json(session_dir, f"fio_core_{core}.json", result)

                job = result["jobs"][0]

                # [수정] Latency 지표 추출 (mean값)
                # clat: 명령이 커널에 전달된 후 완료될 때까지의 시간
                # lat: I/O 생성부터 완료까지의 전체 시간
                lat_ns = job["read"]["lat_ns"]["mean"]
                iops = job["read"]["iops"]
                lat_us = lat_ns / 1000

                self.all_results[disk].append(
                    {"core": core, "lat_us": lat_us, "iops": iops}
                )

                # [요청 1] 턴(Turn)마다 결과 즉시 출력
                print(
                    f"  -> [Core {core:2d} 완료] Avg Latency: {lat_us:8.2f} us | IOPS: {iops:9.0f}"
                )

        # [요청 2] 현재까지 수집된 모든 디스크의 결과를 하나의 테이블로 병합 출력
        self._print_combined_report()

    def _print_combined_report(self):
        print(
            "\n  ======================================================================="
        )
        print("  ▶ [통합 결과] CPU 코어별 단일 코어 최대 성능 리포트 (QD64)")
        print(
            "  ======================================================================="
        )

        disks = list(self.all_results.keys())
        if not disks:
            return

        # 디스크 개수에 맞춰 테이블 헤더를 동적으로 생성
        header = f"  {'Core ID':^7} |"
        for d in disks:
            d_name = d.split("/")[-1]
            header += f" {d_name} Lat(us) | {d_name} IOPS |"
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
                    # 레이아웃 정렬 최적화
                    row_str += f" {res['lat_us']:11.2f} | {res['iops']:10.0f} |"
                else:
                    row_str += "      -      |      -     |"
            print(row_str)

        print(
            "  ======================================================================="
        )
