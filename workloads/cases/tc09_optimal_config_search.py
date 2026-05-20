import os

class Scenario:
    def __init__(self):
        self.tc_name = "TC09_Topology_Aware_Optimal"
        self.description = "시스템의 NUMA 토폴로지와 코어를 매핑하여 영혼까지 끌어모은 최적의 성능을 찾습니다."

    def execute(self, disk, runner_func, reporter, session_dir, numa_node, sys_info=None):
        print(f"\n[Scenario] {self.tc_name} 시작 - 대상: {disk}")
        
        # 1. 대상 디스크의 NUMA 노드 파악
        target_numa = "-1"
        target_cpus = ""
        total_cores = os.cpu_count()
        
        if sys_info and "discovered" in sys_info:
            # 스토리지 리스트에서 현재 디스크 찾기
            for st in sys_info["discovered"].get("storage", []):
                if st["path"] == disk:
                    target_numa = st.get("numa_node", "-1")
                    break
            
            # 해당 NUMA 노드에 속한 CPU 코어 리스트 가져오기
            if target_numa != "-1" and target_numa in sys_info["discovered"].get("numa", {}):
                target_cpus = sys_info["discovered"]["numa"][target_numa]["cpus"]
        
        # NUMA 정보를 찾지 못했거나 가상 장치(ramdisk)인 경우 전체 코어 사용
        if not target_cpus:
            target_cpus = f"0-{total_cores - 1}"
        
        # CPU 코어 개수 계산 (예: "0-19" -> 20개)
        try:
            if "-" in target_cpus:
                start, end = map(int, target_cpus.split("-"))
                core_count = end - start + 1
            else:
                core_count = len(target_cpus.split(","))
        except:
            core_count = total_cores

        print(f"  [*] 타겟 장치 NUMA Node : {target_numa}")
        print(f"  [*] 할당된 최적 코어 목록 : {target_cpus} (총 {core_count}개 코어 1:1 매핑)")

        results = []

        # [Test 1] 일반적인 방식 (OS가 알아서 스케줄링, 단일 스레드 높은 QD)
        print("\n  -> [비교군] OS 기본 스케줄링 방식 측정 중...")
        wl_default = {
            "name": "default_unoptimized",
            "rw": "randread",
            "bs": "4k",
            "iodepth": 128,
            "numjobs": 4, # 임의의 스레드 수
            "runtime": 5,
            "time_based": 1,
            "size": "100%",
            "direct": 1,
            "group_reporting": 1
        }
        res_default = runner_func(disk, wl_default, numa_node=None)
        
        if res_default:
            iops = res_default["jobs"][0]["read"]["iops"]
            lat = res_default["jobs"][0]["read"]["lat_ns"]["mean"] / 1000
            results.append({"mode": "Default (Unoptimized)", "iops": iops, "lat": lat})
            print(f"     결과: IOPS: {iops:9.0f} | Latency: {lat:8.2f} us")

        # [Test 2] Topology-Aware 최적화 방식 (NUMA 매핑 + 코어 1:1 매핑)
        print("\n  -> [최적화] Topology-Aware 매핑 방식 측정 중...")
        wl_optimized = {
            "name": "topology_optimized",
            "rw": "randread",
            "bs": "4k",
            "iodepth": 32, # 각 코어당 적절한 큐 분배
            "numjobs": core_count, # 코어 수와 1:1 매칭
            "cpus_allowed": target_cpus, # 해당 코어에만 I/O 스레드 고정 (컨텍스트 스위칭 제거)
            "runtime": 5,
            "time_based": 1,
            "size": "100%",
            "direct": 1,
            "group_reporting": 1
        }
        
        # NUMA 정책이 지정 가능한 경우 적용
        opt_numa = target_numa if target_numa != "-1" else None
        res_optimized = runner_func(disk, wl_optimized, numa_node=opt_numa)

        if res_optimized:
            iops = res_optimized["jobs"][0]["read"]["iops"]
            lat = res_optimized["jobs"][0]["read"]["lat_ns"]["mean"] / 1000
            results.append({"mode": "Topology-Aware (Optimized)", "iops": iops, "lat": lat})
            print(f"     결과: IOPS: {iops:9.0f} | Latency: {lat:8.2f} us")

        self._print_summary(results)

    def _print_summary(self, results):
        if len(results) < 2: return
        print("\n  =======================================================")
        print("  ▶ [요약] 최적화 전/후 성능 비교 리포트")
        print("  =======================================================")
        print(f"   측정 모드                        |    IOPS    | Avg Latency (us)")
        print("  -------------------------------------------------------")
        for res in results:
            print(f"   {res['mode']:<30} | {res['iops']:10.0f} | {res['lat']:12.2f}")
        
        gain = (results[1]['iops'] / results[0]['iops'] - 1) * 100 if results[0]['iops'] > 0 else 0
        print("  =======================================================")
        print(f"  💡 최적화 매핑을 통해 IOPS가 약 {gain:+.1f}% 향상되었습니다.\n")
