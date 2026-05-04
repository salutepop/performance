import os

class Scenario:
    def __init__(self):
        self.tc_name = "TC06_Mixed_Workload_70_30"
        self.description = "4K 70% 읽기, 30% 쓰기 혼합 부하를 QD별로 측정하여 성능 곡선을 분석합니다."

    def execute(self, disk, runner_func, reporter, session_dir, numa_node):
        print(f"\n[Scenario] {self.tc_name} 시작 - 대상: {disk}")
        
        # 측정하고자 하는 Queue Depth 리스트
        q_depths = [1, 2, 4, 8, 16, 32, 64, 128, 256]
        
        results = []

        for qd in q_depths:
            wl = {
                "name": f"mixed_70_30_qd{qd}",
                "rw": "randrw",
                "rwmixread": 70,    # Read 70%
                "bs": "4k",
                "iodepth": qd,
                "numjobs": 1,
                "runtime": 3,
                "time_based": 1,
                "size": "100%",
                "direct": 1,
                "group_reporting": 1
            }

            print(f"  -> [QD {qd:3d}] 측정 중...")
            result = runner_func(disk, wl, numa_node)

            if result:
                reporter.save_json(session_dir, f"fio_mixed_qd{qd}.json", result)
                
                job = result["jobs"][0]
                # 혼합 부하의 경우 read와 write 지표를 합산하거나 각각 관리해야 함
                read_iops = job["read"]["iops"]
                write_iops = job["write"]["iops"]
                total_iops = read_iops + write_iops
                
                avg_lat_us = job["mixed"]["lat_ns"]["mean"] / 1000 if "mixed" in job else (job["read"]["lat_ns"]["mean"] + job["write"]["lat_ns"]["mean"]) / 2000
                
                results.append({
                    "qd": qd,
                    "total_iops": total_iops,
                    "avg_lat_us": avg_lat_us
                })
                
                print(f"     완료: Total IOPS: {total_iops:9.0f} | Avg Latency: {avg_lat_us:8.2f} us")

        self._print_summary_table(results)

    def _print_summary_table(self, results):
        print("\n  =======================================================")
        print("  ▶ [요약] Mixed Workload (70:30) 성능 결과")
        print("  =======================================================")
        print("     QD   |   Total IOPS   |   Avg Latency (us)")
        print("  -------------------------------------------------------")
        for res in results:
            print(f"    {res['qd']:3d}   |   {res['total_iops']:12.0f} |   {res['avg_lat_us']:16.2f}")
        print("  =======================================================\n")
