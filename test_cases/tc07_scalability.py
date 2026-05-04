import os

class Scenario:
    def __init__(self):
        self.tc_name = "TC07_Performance_Scalability_All"
        self.description = "4대 주요 워크로드(Seq/Rand, R/W)에 대해 장치 개수별 성능 선형성을 측정합니다."
        self.run_all_disks = True

    def execute(self, disks, runner_func, reporter, session_dir, numa_node):
        print(f"\n[Scenario] {self.tc_name} 시작 - 총 장치 수: {len(disks)}")
        
        # 테스트할 워크로드 조합 정의
        test_modes = [
            {"label": "Sequential Read",  "rw": "read",      "bs": "128k", "iodepth": 32},
            {"label": "Sequential Write", "rw": "write",     "bs": "128k", "iodepth": 32},
            {"label": "Random Read",      "rw": "randread",  "bs": "4k",   "iodepth": 64},
            {"label": "Random Write",     "rw": "randwrite", "bs": "4k",   "iodepth": 64},
        ]

        for mode in test_modes:
            print(f"\n" + "="*60)
            print(f"▶ [워크로드 측정] {mode['label']} (BS={mode['bs']}, QD={mode['iodepth']})")
            print("="*60)
            
            results = []
            baseline_bw = 0
            baseline_iops = 0

            for i in range(1, len(disks) + 1):
                target_group = disks[:i]
                group_label = f"{mode['rw']}_{i}_disks"
                
                print(f"  -> [Step] 장치 {i}개 테스트 중...")
                
                wl = {
                    "name": group_label,
                    "rw": mode["rw"],
                    "bs": mode["bs"],
                    "iodepth": mode["iodepth"],
                    "numjobs": i, # 단일 코어/세션 기준 성능 확인을 위해 1 유지 (필요시 i로 변경 가능)
                    "runtime": 1,  # 빠른 테스트를 위해 3초로 설정
                    "time_based": 1,
                    "size": "100%",
                    "direct": 1,
                    "group_reporting": 1
                }

                result = runner_func(target_group, wl, numa_node)

                if result:
                    reporter.save_json(session_dir, f"fio_{group_label}.json", result)
                    
                    job = result["jobs"][0]
                    mode_key = "read" if "read" in mode["rw"] else "write"
                    bw_mb = job[mode_key]["bw"] / 1024
                    iops = job[mode_key]["iops"]
                    
                    if i == 1:
                        baseline_bw = bw_mb
                        baseline_iops = iops
                    
                    bw_scaling = bw_mb / baseline_bw if baseline_bw > 0 else 0
                    iops_scaling = iops / baseline_iops if baseline_iops > 0 else 0
                    
                    results.append({
                        "count": i,
                        "bw_mb": bw_mb,
                        "iops": iops,
                        "bw_scaling": bw_scaling,
                        "iops_scaling": iops_scaling
                    })
                    print(f"     완료: BW: {bw_mb:8.2f} MB/s ({bw_scaling:.2f}x) | IOPS: {iops:9.0f}")

            self._print_summary_table(mode["label"], results)

    def _print_summary_table(self, label, results):
        print(f"\n  [요약 리포트: {label}]")
        print("  " + "-"*75)
        print("   장치 수 |   BW (MB/s)  |  BW Scaling |    IOPS    | IOPS Scaling")
        print("  " + "-"*75)
        for res in results:
            print(f"     {res['count']:2d}    |  {res['bw_mb']:11.2f} |    {res['bw_scaling']:6.2f}x   | {res['iops']:10.0f} |    {res['iops_scaling']:6.2f}x")
        print("  " + "-"*75 + "\n")
