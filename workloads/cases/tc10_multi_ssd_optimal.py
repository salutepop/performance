import os

class Scenario:
    def __init__(self):
        self.tc_name = "TC10_Multi_SSD_Optimal_Search"
        self.description = "시스템에 인식된 모든 SSD를 하나로 묶어(Multi-SSD), 전체 시스템의 한계 IOPS 및 대역폭을 측정합니다."
        self.run_all_disks = True

    def execute(self, disks, runner_func, reporter, session_dir, numa_node, sys_info=None):
        print(f"\n[Scenario] {self.tc_name} 시작 - 총 장치 수: {len(disks)}대")
        
        # 전체 시스템 코어 수 파악
        total_cores = os.cpu_count()
        if sys_info and "discovered" in sys_info:
            total_cores = sys_info["discovered"]["cpu"].get("total_cores", total_cores)
        
        target_cpus = f"0-{total_cores - 1}"

        print(f"  [*] 타겟 장치 목록 : {', '.join(disks)}")
        print(f"  [*] 동원 가능한 전체 코어 : {total_cores}개 ({target_cpus})")

        results = []

        # [Test 1] 일반적인 멀티 디스크 부하 방식 (단순히 코어만 100% 쓴 경우)
        print("\n  -> [비교군] 단순 전체 코어 동원 방식 (OS 자율 스케줄링) 측정 중...")
        wl_default = {
            "name": "multi_default_all_cores",
            "rw": "randread",
            "bs": "4k",
            "iodepth": 32,
            "numjobs": total_cores, # 사용자의 지적대로 비교군도 코어를 100% 씁니다.
            "runtime": 5,
            "time_based": 1,
            "size": "100%",
            "direct": 1,
            "group_reporting": 1
        }
        res_default = runner_func(disks, wl_default, numa_node=None)
        
        if res_default:
            iops = res_default["jobs"][0]["read"]["iops"]
            lat = res_default["jobs"][0]["read"]["lat_ns"]["mean"] / 1000
            bw = res_default["jobs"][0]["read"]["bw"] / 1024
            results.append({"mode": "Default (All-Cores, No Isolation)", "iops": iops, "lat": lat, "bw": bw})
            print(f"     결과: BW: {bw:8.2f} MB/s | IOPS: {iops:9.0f} | Latency: {lat:8.2f} us")

        # [Test 2] True Topology & Isolation 최적화
        print("\n  -> [최적화] 기기별 코어 격리(Core Isolation) 및 NUMA 매핑 측정 중...")
        
        fio_config = f"""
[global]
ioengine=libaio
direct=1
rw=randread
bs=4k
iodepth=32
runtime=5
time_based=1
group_reporting=1
size=100%
"""
        # 디스크별로 코어를 쪼개서 전담 마크(Isolation) 설정
        cores_per_disk = max(1, total_cores // len(disks))
        
        for idx, disk in enumerate(disks):
            # 실제 NVMe인 경우 NUMA 노드 기반으로 코어 매핑 시도
            target_cpus = ""
            if sys_info and "discovered" in sys_info:
                for st in sys_info["discovered"].get("storage", []):
                    if st["path"] == disk:
                        numa = st.get("numa_node", "-1")
                        if numa != "-1" and numa in sys_info["discovered"].get("numa", {}):
                            target_cpus = sys_info["discovered"]["numa"][numa]["cpus"]
                        break
            
            # NUMA 정보가 없거나 램디스크인 경우 인위적으로 코어를 격리 분할
            if not target_cpus:
                start_core = idx * cores_per_disk
                end_core = min(total_cores - 1, start_core + cores_per_disk - 1)
                target_cpus = f"{start_core}-{end_core}"
            
            fio_config += f"\n[disk_{idx}]\n"
            fio_config += f"filename={disk}\n"
            fio_config += f"numjobs={cores_per_disk}\n"
            fio_config += f"cpus_allowed={target_cpus}\n"

        config_path = os.path.join(session_dir, "optimized_multi.fio")
        with open(config_path, "w") as f:
            f.write(fio_config)

        # 사용자에게 어떤 조건으로 최적화되었는지 명확히 출력
        print("\n     [적용된 Topology & Isolation 매핑]")
        for idx, disk in enumerate(disks):
            # 설정 파일에서 cpus_allowed 파싱해서 출력
            lines = fio_config.split(f"[disk_{idx}]")[1].split("[disk_")[0].strip().split("\n")
            cpus = [l.split("=")[1] for l in lines if l.startswith("cpus_allowed")][0]
            jobs = [l.split("=")[1] for l in lines if l.startswith("numjobs")][0]
            numa_str = " (가상 장치/NUMA 정보 없음 -> 인위적 격리)" if sys_info and sys_info.get("discovered", {}).get("storage", []) and all(s.get("numa_node", "-1") == "-1" for s in sys_info.get("discovered", {}).get("storage", []) if s["path"] == disk) else " (NUMA Local 매핑)"
            print(f"      - {disk:<12} : 코어 {cpus:<10} 할당 (Thread {jobs}개){numa_str}")
        print(f"      => 총 {cores_per_disk * len(disks)}개의 스레드가 장치별로 완벽히 격리되어 동시에 실행됩니다.\n")

        import subprocess
        import json
        try:
            # 설정 파일이 아닌 명령어 인자로 json 출력 포맷 지정
            res = subprocess.run(["sudo", "fio", "--output-format=json", config_path], capture_output=True, text=True, check=True)
            res_optimized = json.loads(res.stdout)
            
            iops = res_optimized["jobs"][0]["read"]["iops"]
            lat = res_optimized["jobs"][0]["read"]["lat_ns"]["mean"] / 1000
            bw = res_optimized["jobs"][0]["read"]["bw"] / 1024
            results.append({"mode": "Optimized (Core Isolation/NUMA)", "iops": iops, "lat": lat, "bw": bw})
            print(f"     결과: BW: {bw:8.2f} MB/s | IOPS: {iops:9.0f} | Latency: {lat:8.2f} us")
        except Exception as e:
            print(f"  [Error] 최적화 런 실패: {e}")

        self._print_summary(results, len(disks))

    def _print_summary(self, results, disk_count):
        if len(results) < 2: return
        print("\n  =======================================================================")
        print(f"  ▶ [요약] Multi-SSD ({disk_count}대) 최적화 전/후 성능 비교 리포트")
        print("  =======================================================================")
        print(f"   측정 모드                        |   BW (MB/s) |    IOPS    | Avg Latency (us)")
        print("  -----------------------------------------------------------------------")
        for res in results:
            print(f"   {res['mode']:<30} | {res['bw']:11.2f} | {res['iops']:10.0f} | {res['lat']:12.2f}")
        
        gain_iops = (results[1]['iops'] / results[0]['iops'] - 1) * 100 if results[0]['iops'] > 0 else 0
        gain_bw = (results[1]['bw'] / results[0]['bw'] - 1) * 100 if results[0]['bw'] > 0 else 0
        
        print("  =======================================================================")
        print(f"  💡 전체 코어 동원 최적화를 통해 IOPS가 약 {gain_iops:+.1f}%, 대역폭이 {gain_bw:+.1f}% 향상되었습니다.\n")
