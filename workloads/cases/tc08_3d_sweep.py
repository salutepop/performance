import os

class Scenario:
    def __init__(self):
        self.tc_name = "TC08_3D_Parameter_Sweep"
        self.description = "SSD 개수, Job 수, QD 조합에 따른 성능 변화를 전수 조사합니다."
        self.run_all_disks = True

    def execute(self, disks, runner_func, reporter, session_dir, numa_node, sys_info=None):
        print(f"\n[Scenario] {self.tc_name} 시작")
        
        # [수정] 발견된 시스템 정보를 바탕으로 테스트 범위 자동 결정
        total_cores = os.cpu_count()
        if sys_info and "discovered" in sys_info:
            total_cores = sys_info["discovered"]["cpu"].get("total_cores", total_cores)
        
        disk_counts = range(1, len(disks) + 1)
        # 코어 수에 따라 1, 2, 4, 8... 순으로 스레드 수 결정 (최대 코어 수까지)
        job_counts = [1]
        while job_counts[-1] * 2 <= total_cores:
            job_counts.append(job_counts[-1] * 2)
        if total_cores not in job_counts:
            job_counts.append(total_cores)

        q_depths = [1, 4, 16, 64] # 큐 뎁스
        
        all_results = {}

        for d_cnt in disk_counts:
            target_disks = disks[:d_cnt]
            all_results[d_cnt] = []
            
            print(f"\n" + "="*70)
            print(f"▶ [장치 수: {d_cnt}개] 테스트 시작")
            print("="*70)

            for jobs in job_counts:
                for qd in q_depths:
                    # 각 Job당 QD를 배분 (fio의 iodepth는 job당 적용됨)
                    wl_name = f"d{d_cnt}_j{jobs}_qd{qd}"
                    print(f"  -> [Jobs: {jobs:2d}, QD: {qd:3d}] 측정 중...", end="\r")
                    
                    wl = {
                        "name": wl_name,
                        "rw": "randread",
                        "bs": "4k",
                        "iodepth": qd,
                        "numjobs": jobs,
                        "runtime": 2, # 빠른 스윕을 위해 2초 설정
                        "time_based": 1,
                        "size": "100%",
                        "direct": 1,
                        "group_reporting": 1
                    }

                    result = runner_func(target_disks, wl, numa_node)

                    if result:
                        job_data = result["jobs"][0]
                        iops = job_data["read"]["iops"]
                        lat_us = job_data["read"]["lat_ns"]["mean"] / 1000
                        bw_mb = job_data["read"]["bw"] / 1024
                        
                        all_results[d_cnt].append({
                            "jobs": jobs,
                            "qd": qd,
                            "iops": iops,
                            "lat_us": lat_us,
                            "bw_mb": bw_mb
                        })
            
            self._print_disk_report(d_cnt, all_results[d_cnt])

    def _print_disk_report(self, d_cnt, results):
        print(f"\n\n  [장치 {d_cnt}개 성능 매트릭스 (IOPS)]")
        print("  " + "-"*60)
        
        # QD 헤더 출력
        qds = sorted(list(set(r["qd"] for r in results)))
        header = "   Jobs |" + "".join([f"  QD {q:3d}  |" for q in qds])
        print(header)
        print("  " + "-"*len(header))
        
        # Job 수별로 행 출력
        jobs_list = sorted(list(set(r["jobs"] for r in results)))
        for j in jobs_list:
            row = f"    {j:2d}  |"
            for q in qds:
                # 해당 조합의 결과 찾기
                match = next((r for r in results if r["jobs"] == j and r["qd"] == q), None)
                if match:
                    row += f" {match['iops']:8.0f} |"
                else:
                    row += "    -     |"
            print(row)
        print("  " + "-"*len(header) + "\n")
