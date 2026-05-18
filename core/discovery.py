import os
import json
import subprocess
import re

class SystemDiscovery:
    def __init__(self):
        self.info = {
            "cpu": {},
            "numa": {},
            "storage": [],
            "nvme_ctrls": [],
            "memory": {},
            "gpu": []
        }

    def discover_all(self):
        self._discover_cpu()
        self._discover_numa()
        self._discover_storage()
        self._discover_nvme_ctrls()
        self._discover_memory()
        self._discover_gpu()
        return self.info

    def _discover_cpu(self):
        try:
            self.info["cpu"]["total_cores"] = os.cpu_count()
            # lscpu를 통해 아키텍처 및 상세 정보 획득
            res = subprocess.check_output("lscpu", shell=True, text=True)
            for line in res.splitlines():
                if "Model name" in line:
                    self.info["cpu"]["model"] = line.split(":")[1].strip()
                if "Thread(s) per core" in line:
                    self.info["cpu"]["threads_per_core"] = int(line.split(":")[1].strip())
        except:
            pass

    def _discover_numa(self):
        # /sys/devices/system/node/ 에서 NUMA 정보 수집
        numa_info = {}
        try:
            nodes = sorted([d for d in os.listdir("/sys/devices/system/node") if d.startswith("node")])
            for node in nodes:
                node_id = node.replace("node", "")
                cpulist_path = f"/sys/devices/system/node/{node}/cpulist"
                with open(cpulist_path, "r") as f:
                    numa_info[node_id] = {"cpus": f.read().strip()}
            self.info["numa"] = numa_info
        except:
            pass

    def _discover_storage(self):
        # nvme 장치 탐색 및 NUMA Affinity 확인
        try:
            nvme_dirs = [d for d in os.listdir("/sys/class/nvme") if d.startswith("nvme")]
            for nvme in nvme_dirs:
                # nvme0n1 형태의 네임스페이스 탐색
                dev_path = f"/sys/class/nvme/{nvme}"
                namespaces = [d for d in os.listdir(dev_path) if d.startswith(nvme + "n")]
                
                # NUMA Node Affinity 확인
                numa_node = "-1"
                try:
                    with open(f"{dev_path}/device/numa_node", "r") as f:
                        numa_node = f.read().strip()
                except: pass

                for ns in namespaces:
                    self.info["storage"].append({
                        "name": f"/dev/{ns}",
                        "ctrl": nvme,
                        "numa_node": numa_node,
                        "path": f"/dev/{ns}"
                    })
        except:
            # NVMe가 없을 경우 기본 디스크 탐색 (lsblk)
            pass

    def _discover_nvme_ctrls(self):
        """NVMe 컨트롤러 단위 정적 sysfs attr 수집 — topology.json에 보존되어
        post-hoc 분석 시 디바이스 모델/펌웨어/큐 수 등 컨텍스트 제공."""
        attrs = ["model", "state", "firmware_rev", "serial", "transport",
                 "address", "cntrltype", "queue_count", "numa_node", "subsysnqn"]
        try:
            nvme_dirs = sorted(d for d in os.listdir("/sys/class/nvme") if d.startswith("nvme"))
        except Exception:
            return
        for ctrl in nvme_dirs:
            ctx = {"name": ctrl}
            for a in attrs:
                p = f"/sys/class/nvme/{ctrl}/{a}"
                try:
                    with open(p) as f:
                        ctx[a] = f.read().strip()
                except Exception:
                    pass
            # queue_count 정수 변환 시도
            if "queue_count" in ctx:
                try:
                    ctx["queue_count"] = int(ctx["queue_count"])
                except ValueError:
                    pass
            self.info["nvme_ctrls"].append(ctx)

    def _discover_memory(self):
        try:
            # 메모리 총량 및 속도 (dmidecode는 root 권한 필요하므로 예외처리)
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if "MemTotal" in line:
                        self.info["memory"]["total_gb"] = round(int(line.split()[1]) / (1024*1024), 2)
            
            # dmidecode 시도
            res = subprocess.check_output("sudo dmidecode -t memory | grep -E 'Size|Speed|Type' | grep -v 'No Module'", shell=True, text=True)
            self.info["memory"]["details"] = res.strip().split("\n")
        except:
            self.info["memory"]["details"] = "Permission denied or tool missing"

    def _discover_gpu(self):
        """nvidia-smi -L로 GPU 목록 탐지. 없으면 빈 리스트로 남김.
        각 GPU는 PCI bus_id를 통해 /sys/bus/pci/devices/.../numa_node로 NUMA 매핑."""
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=index,name,pci.bus_id",
                 "--format=csv,noheader"],
                text=True, stderr=subprocess.DEVNULL, timeout=2
            )
        except Exception:
            return

        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            idx, name, pci_bus = parts[0], parts[1], parts[2]
            numa_node = "-1"
            try:
                # nvidia-smi 형식 "00000000:01:00.0" (8-hex 도메인) → sysfs는 4-hex 도메인 소문자.
                parts_pci = pci_bus.lower().split(":")
                if len(parts_pci) == 3:
                    domain = parts_pci[0][-4:].rjust(4, "0")
                    pci_norm = f"{domain}:{parts_pci[1]}:{parts_pci[2]}"
                else:
                    pci_norm = pci_bus.lower()
                sysfs = f"/sys/bus/pci/devices/{pci_norm}/numa_node"
                if os.path.exists(sysfs):
                    with open(sysfs) as f:
                        numa_node = f.read().strip()
            except Exception:
                pass
            self.info["gpu"].append({
                "index": int(idx) if idx.isdigit() else idx,
                "name": name,
                "pci_bus_id": pci_bus,
                "numa_node": numa_node,
            })

    def save_to_file(self, filepath="config/discovered_system.json"):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.info, f, indent=4)
        print(f"[*] 시스템 정보 저장 완료: {filepath}")
