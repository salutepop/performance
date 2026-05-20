"""
SystemMonitor — /proc·/sys·nvidia-smi 기반 통합 시스템 메트릭 수집기.

eBPF와 독립이라 ARM/x86, GPU 0/1/N, NUMA 유무에 무관하게 동작.
1초 주기로 1행씩 system_metrics.csv에 쌓고, 세션 시작 시 topology.json을 한 번 dump.

산출물:
  {output_dir}/topology.json
  {output_dir}/system_metrics.csv
"""

import csv
import datetime
import json
import os
import re
import subprocess
import threading
import time

from ..discovery import SystemDiscovery


NVME_RE = re.compile(r"^(nvme\d+)q\d+$")


class SystemMonitor:
    def __init__(self, output_dir, session_id=None, interval=1.0, sys_info=None):
        self.output_dir = output_dir
        self.session_id = session_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.interval = float(interval)
        self.sys_info = sys_info  # 외부에서 이미 discover한 정보가 있으면 재사용

        self._stop_evt = threading.Event()
        self._poll_thread = None
        self._gpu_thread = None
        self._gpu_proc = None
        self._gpu_lock = threading.Lock()
        self._latest_gpu = {}  # {gpu_idx: {col: value}}

        self._csv_file = None
        self._csv_writer = None

        # delta 계산용 prev snapshot
        self._prev_cpu = None  # {cpu_id: list of jiffies}
        self._prev_irq = None  # {(irq_num, cpu_id): count}
        self._prev_ts = None
        self._warmed_up = False  # 첫 tick(0~1s)은 버리고 baseline만 갱신

        # topology
        self._cpu_to_node = {}
        self._nodes = []
        self._nvme_controllers = []  # ["nvme0", "nvme1", ...]
        self._gpu_indices = []  # [0, 1, ...]

        self.columns = []  # CSV 컬럼 순서

    # ----------------------------------------------------------------------
    # public API
    def start(self):
        os.makedirs(self.output_dir, exist_ok=True)

        if self.sys_info is None:
            self.sys_info = SystemDiscovery().discover_all()

        self._build_topology()
        self._dump_topology()
        self._build_columns()

        csv_path = os.path.join(self.output_dir, f"system_metrics_{self.session_id}.csv")
        self._csv_file = open(csv_path, "w", newline="")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=self.columns)
        self._csv_writer.writeheader()

        # delta baseline 채우기
        self._prev_cpu = self._read_proc_stat()
        self._prev_irq = self._read_proc_interrupts_raw()
        self._prev_ts = time.monotonic()

        # GPU 있으면 dmon streaming subprocess + reader thread
        if self._gpu_indices:
            self._start_gpu_reader()

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        print(f"[SysMon] started → {csv_path} (nodes={len(self._nodes)}, "
              f"nvme={len(self._nvme_controllers)}, gpu={len(self._gpu_indices)})")

    def stop(self):
        self._stop_evt.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=self.interval * 2 + 1)
        if self._gpu_proc:
            try:
                self._gpu_proc.terminate()
                self._gpu_proc.wait(timeout=2)
            except Exception:
                pass
        if self._gpu_thread:
            self._gpu_thread.join(timeout=2)
        if self._csv_file:
            self._csv_file.close()
        print("[SysMon] stopped")

    # ----------------------------------------------------------------------
    # topology / schema
    def _build_topology(self):
        # NUMA: discovered.numa = {"0": {"cpus": "0-9,20-29"}, "1": {...}}
        numa = self.sys_info.get("discovered", self.sys_info).get("numa", {})
        if not numa:
            # NUMA 정보 없으면 단일 노드로 취급 (모든 CPU를 node "all")
            total = os.cpu_count() or 1
            self._nodes = ["all"]
            self._cpu_to_node = {cpu: "all" for cpu in range(total)}
        else:
            self._nodes = sorted(numa.keys(), key=lambda x: int(x) if x.isdigit() else x)
            for node_id, info in numa.items():
                for cpu in _parse_cpu_list(info.get("cpus", "")):
                    self._cpu_to_node[cpu] = node_id

        # NVMe 컨트롤러 (storage 리스트에서 ctrl 추출, 중복 제거)
        storage = self.sys_info.get("discovered", self.sys_info).get("storage", [])
        ctrls = []
        for s in storage:
            c = s.get("ctrl") or s.get("name", "").split("/")[-1].rstrip("0123456789n")
            if c and c not in ctrls:
                ctrls.append(c)
        # 못 찾으면 /proc/interrupts에서 fallback 탐색
        if not ctrls:
            ctrls = sorted({m.group(1) for m in (NVME_RE.match(_irq_name(line)) for line in _proc_lines("/proc/interrupts")) if m})
        self._nvme_controllers = ctrls

        # GPU
        gpus = self.sys_info.get("discovered", self.sys_info).get("gpu", [])
        self._gpu_indices = [g["index"] for g in gpus if isinstance(g.get("index"), int)]

        # cpufreq 존재 여부 1회 탐지 (없으면 컬럼/수집 모두 스킵).
        self._cpufreq_paths = {}
        for cpu_id in self._cpu_to_node:
            p = f"/sys/devices/system/cpu/cpu{cpu_id}/cpufreq/scaling_cur_freq"
            if os.path.exists(p):
                self._cpufreq_paths[cpu_id] = p
        self._has_cpufreq = bool(self._cpufreq_paths)

        # per-NUMA meminfo 존재 여부. 단일 노드 fallback("all")인 경우 글로벌 meminfo 사용
        # 이미 있으므로 sysfs 확인 안 함.
        self._numa_meminfo_paths = {}
        for node in self._nodes:
            if node == "all":
                continue
            p = f"/sys/devices/system/node/node{node}/meminfo"
            if os.path.exists(p):
                self._numa_meminfo_paths[node] = p
        self._has_numa_meminfo = bool(self._numa_meminfo_paths)

        # NVMe controller → PCI AER counter 파일 경로 매핑. 디바이스가 AER 미지원이면 skip.
        ctrl_addrs = {}  # {ctrl_name: pci_addr} from discovered.nvme_ctrls
        for c in self.sys_info.get("discovered", self.sys_info).get("nvme_ctrls", []) or []:
            name = c.get("name")
            addr = c.get("address")
            if name and addr:
                ctrl_addrs[name] = addr
        self._aer_paths = {}  # {ctrl: {'cor': path, 'fatal': path, 'nonfatal': path}}
        for ctrl in self._nvme_controllers:
            addr = ctrl_addrs.get(ctrl)
            if not addr:
                continue
            base = f"/sys/bus/pci/devices/{addr}"
            paths = {}
            for kind, fname in (("cor", "aer_dev_correctable"),
                                ("fatal", "aer_dev_fatal"),
                                ("nonfatal", "aer_dev_nonfatal")):
                p = os.path.join(base, fname)
                if os.path.exists(p):
                    paths[kind] = p
            if paths:
                self._aer_paths[ctrl] = paths
        self._has_aer = bool(self._aer_paths)

        # 네트워크 통계는 opt-in (NVMe-oF/RDMA 환경 한정). 환경변수 PMON_ENABLE_NET=1로 활성.
        self._net_enabled = os.environ.get("PMON_ENABLE_NET", "0") in ("1", "true", "yes")
        self._net_ifaces = []
        if self._net_enabled:
            try:
                with open("/proc/net/dev") as f:
                    lines = f.readlines()[2:]
                for line in lines:
                    iface = line.split(":")[0].strip()
                    if not iface or iface == "lo":
                        continue
                    # docker/bridge/veth 등 가상 인터페이스 제외, 물리/RDMA만 남김
                    if iface.startswith(("docker", "br-", "veth", "virbr")):
                        continue
                    self._net_ifaces.append(iface)
            except Exception:
                pass
        self._prev_net = None  # {iface: (rx_bytes, tx_bytes)} 인터벌 delta용

    def _dump_topology(self):
        path = os.path.join(self.output_dir, f"topology_{self.session_id}.json")
        with open(path, "w") as f:
            json.dump({
                "session_id": self.session_id,
                "nodes": self._nodes,
                "cpu_to_node": self._cpu_to_node,
                "nvme_controllers": self._nvme_controllers,
                "gpus": self.sys_info.get("discovered", self.sys_info).get("gpu", []),
                "raw": self.sys_info,
            }, f, indent=2, default=str)

    def _build_columns(self):
        cols = ["timestamp"]
        for node in self._nodes:
            for k in ("user", "sys", "iowait", "irq", "softirq"):
                cols.append(f"node{node}_{k}_pct")
        for ctrl in self._nvme_controllers:
            cols += [f"{ctrl}_irq_per_s", f"{ctrl}_top_cpu", f"{ctrl}_top_cpu_node",
                     f"{ctrl}_active_queues", f"{ctrl}_active_cpus"]
        cols += [
            "mem_available_mb", "mem_dirty_mb", "mem_writeback_mb", "swap_used_mb",
            "loadavg_1m",
        ]
        if self._has_cpufreq:
            for node in self._nodes:
                cols += [f"node{node}_freq_avg_mhz", f"node{node}_freq_max_mhz"]
        if self._has_numa_meminfo:
            for node in self._numa_meminfo_paths:
                cols += [f"node{node}_mem_free_mb", f"node{node}_mem_used_mb"]
        if self._has_aer:
            for ctrl in self._aer_paths:
                cols += [f"{ctrl}_aer_cor", f"{ctrl}_aer_fatal", f"{ctrl}_aer_nonfatal"]
        if self._net_enabled and self._net_ifaces:
            for iface in self._net_ifaces:
                cols += [f"net_{iface}_rx_mb_s", f"net_{iface}_tx_mb_s"]
        for idx in self._gpu_indices:
            cols += [
                f"gpu{idx}_pwr_w", f"gpu{idx}_temp_c",
                f"gpu{idx}_sm_pct", f"gpu{idx}_mem_pct",
                f"gpu{idx}_mem_used_mb",
                f"gpu{idx}_pcie_rx_mb_s", f"gpu{idx}_pcie_tx_mb_s",
            ]
        self.columns = cols

    # ----------------------------------------------------------------------
    # collectors (raw reads)
    def _read_proc_stat(self):
        """Return {cpu_id: [user, nice, sys, idle, iowait, irq, softirq, steal, ...]}."""
        out = {}
        for line in _proc_lines("/proc/stat"):
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            parts = line.split()
            name = parts[0]
            try:
                cpu_id = int(name[3:])
            except ValueError:
                continue
            out[cpu_id] = [int(x) for x in parts[1:]]
        return out

    def _read_proc_interrupts_raw(self):
        """Return {(irq_num, cpu_id): count, ...} only for nvme*q* rows."""
        out = {}
        for line in _proc_lines("/proc/interrupts"):
            line = line.rstrip()
            if ":" not in line:
                continue
            head, rest = line.split(":", 1)
            irq_num = head.strip()
            tokens = rest.split()
            # tail: type + chip + level + name → name == 마지막 토큰
            if not tokens:
                continue
            name = tokens[-1]
            m = NVME_RE.match(name)
            if not m:
                continue
            ctrl = m.group(1)
            # tokens 앞쪽이 per-CPU counts. 어디까지가 숫자인지 판정
            counts = []
            for t in tokens:
                if t.isdigit():
                    counts.append(int(t))
                else:
                    break
            for cpu_id, c in enumerate(counts):
                out[(ctrl, irq_num, cpu_id)] = c
        return out

    def _read_meminfo(self):
        out = {}
        for line in _proc_lines("/proc/meminfo"):
            k, _, v = line.partition(":")
            v = v.strip().split()
            if v:
                try:
                    out[k.strip()] = int(v[0])  # kB
                except ValueError:
                    pass
        return out

    def _read_loadavg(self):
        try:
            with open("/proc/loadavg") as f:
                return float(f.read().split()[0])
        except Exception:
            return 0.0

    def _read_net_raw(self):
        """{iface: (rx_bytes, tx_bytes)} — /proc/net/dev raw 누적값."""
        out = {}
        try:
            with open("/proc/net/dev") as f:
                lines = f.readlines()[2:]
            for line in lines:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                if iface not in self._net_ifaces:
                    continue
                parts = rest.split()
                if len(parts) < 16:
                    continue
                try:
                    rx = int(parts[0])    # bytes
                    tx = int(parts[8])    # bytes
                    out[iface] = (rx, tx)
                except (ValueError, IndexError):
                    pass
        except Exception:
            pass
        return out

    def _read_aer(self):
        """{ctrl: {cor, fatal, nonfatal}} - PCI AER TOTAL counters (raw 누적값).
        파일 마지막 라인이 'TOTAL_ERR_<KIND> <N>' 형식."""
        out = {}
        for ctrl, paths in self._aer_paths.items():
            vals = {}
            for kind, p in paths.items():
                try:
                    with open(p) as f:
                        last = 0
                        for line in f:
                            parts = line.split()
                            if len(parts) >= 2 and parts[0].startswith("TOTAL_ERR_"):
                                try:
                                    last = int(parts[1])
                                except ValueError:
                                    pass
                        vals[kind] = last
                except Exception:
                    pass
            if vals:
                out[ctrl] = vals
        return out

    def _read_numa_meminfo_mb(self):
        """{node_id: {'free_mb': float, 'used_mb': float}}. 노드별 없으면 빈 dict."""
        out = {}
        for node, path in self._numa_meminfo_paths.items():
            free_kb = used_kb = None
            try:
                with open(path) as f:
                    for line in f:
                        # 형식: "Node <N> MemFree:       <kB> kB"
                        parts = line.split()
                        if len(parts) < 5:
                            continue
                        key = parts[2].rstrip(":")
                        try:
                            val = int(parts[3])
                        except ValueError:
                            continue
                        if key == "MemFree":
                            free_kb = val
                        elif key == "MemUsed":
                            used_kb = val
                        if free_kb is not None and used_kb is not None:
                            break
            except Exception:
                continue
            out[node] = {
                "free_mb": round(free_kb / 1024.0, 1) if free_kb is not None else None,
                "used_mb": round(used_kb / 1024.0, 1) if used_kb is not None else None,
            }
        return out

    def _read_cpu_freq_mhz(self):
        """{cpu_id: mhz} — cpufreq 없는 시스템은 빈 dict."""
        out = {}
        for cpu_id, path in self._cpufreq_paths.items():
            try:
                with open(path) as f:
                    out[cpu_id] = int(f.read().strip()) / 1000.0  # kHz → MHz
            except Exception:
                pass
        return out

    # ----------------------------------------------------------------------
    # GPU streaming
    def _start_gpu_reader(self):
        gpu_csv = ",".join(str(i) for i in self._gpu_indices)
        # stdbuf -oL로 dmon stdout을 강제로 line-buffer → pipe로 라인 즉시 전달.
        try:
            # -c 옵션은 빼면 무한 스트리밍 (nvidia-smi dmon에서 -c 0은 invalid).
            self._gpu_proc = subprocess.Popen(
                ["stdbuf", "-oL",
                 "nvidia-smi", "dmon", "-s", "pumt", "-o", "T",
                 "-i", gpu_csv, "-d", str(int(max(1, self.interval)))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except Exception as e:
            print(f"[SysMon] nvidia-smi dmon start failed: {e}; GPU collection disabled")
            self._gpu_proc = None
            return
        self._gpu_thread = threading.Thread(target=self._gpu_reader_loop, daemon=True)
        self._gpu_thread.start()

    def _gpu_reader_loop(self):
        """dmon -s pumt -o T 라인 파싱:
        #Time     gpu  pwr  gtemp  mtemp  sm  mem  enc  dec  jpg  ofa  fb  bar1  ccpm  rxpci  txpci
        HH:MM:SS    0    5    43    -    3    0    0    0    0    0    -    -    0    -    -
        """
        assert self._gpu_proc is not None
        col_order = None
        while not self._stop_evt.is_set():
            line = self._gpu_proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                # 첫 헤더 라인에서 컬럼 순서 추출
                if col_order is None and line.lower().startswith("#time"):
                    col_order = line.lstrip("#").split()  # ["Time","gpu","pwr","gtemp",...]
                continue
            if col_order is None:
                continue
            parts = line.split()
            if len(parts) != len(col_order):
                continue
            row = dict(zip(col_order, parts))
            try:
                idx = int(row.get("gpu", "-1"))
            except ValueError:
                continue

            def num(key):
                v = row.get(key, "-")
                if v == "-" or v == "":
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None

            with self._gpu_lock:
                self._latest_gpu[idx] = {
                    "pwr": num("pwr"),
                    "gtemp": num("gtemp"),
                    "sm": num("sm"),
                    "mem": num("mem"),
                    "fb": num("fb"),
                    "rxpci": num("rxpci"),
                    "txpci": num("txpci"),
                }

    # ----------------------------------------------------------------------
    # poll loop
    def _poll_loop(self):
        next_tick = time.monotonic() + self.interval
        while not self._stop_evt.wait(max(0, next_tick - time.monotonic())):
            try:
                self._tick()
            except Exception as e:
                print(f"[SysMon] tick error: {e}")
            next_tick += self.interval

    def _tick(self):
        now_ts = time.monotonic()
        elapsed = max(1e-3, now_ts - self._prev_ts)

        cpu_now = self._read_proc_stat()
        irq_now = self._read_proc_interrupts_raw()
        meminfo = self._read_meminfo()
        load1 = self._read_loadavg()

        # ---- CPU per-node ----
        node_agg = {n: {"user": 0, "sys": 0, "iowait": 0, "irq": 0, "softirq": 0, "total": 0} for n in self._nodes}
        for cpu_id, cur in cpu_now.items():
            prev = self._prev_cpu.get(cpu_id)
            if not prev:
                continue
            delta = [c - p for c, p in zip(cur, prev)]
            # delta: [user, nice, sys, idle, iowait, irq, softirq, steal, ...]
            if len(delta) < 7:
                continue
            user, nice, sys_, idle, iowait, irq_, softirq = delta[:7]
            steal = delta[7] if len(delta) > 7 else 0
            total = sum(max(0, d) for d in delta)
            if total <= 0:
                continue
            node = self._cpu_to_node.get(cpu_id, self._nodes[0] if self._nodes else "all")
            agg = node_agg.get(node)
            if agg is None:
                continue
            agg["user"] += user
            agg["sys"] += sys_
            agg["iowait"] += iowait
            agg["irq"] += irq_
            agg["softirq"] += softirq
            agg["total"] += total

        # ---- NVMe IRQ aggregation per controller ----
        nvme_metrics = {ctrl: {"delta": 0, "top_cpu": None, "top_cpu_val": -1,
                                "active_queues": set(), "active_cpus": set()}
                        for ctrl in self._nvme_controllers}
        per_cpu_delta = {}  # {(ctrl, cpu_id): total_delta}
        for key, cur in irq_now.items():
            prev = self._prev_irq.get(key, 0)
            d = cur - prev
            if d <= 0:
                continue
            ctrl, irq_num, cpu_id = key
            per_cpu_delta[(ctrl, cpu_id)] = per_cpu_delta.get((ctrl, cpu_id), 0) + d
            m = nvme_metrics.get(ctrl)
            if m is not None:
                m["active_queues"].add(irq_num)  # 활성 큐 (IRQ 1개 = 큐 1개)
                m["active_cpus"].add(cpu_id)     # IRQ 받은 CPU 집합
        for (ctrl, cpu_id), d in per_cpu_delta.items():
            m = nvme_metrics.get(ctrl)
            if m is None:
                continue
            m["delta"] += d
            if d > m["top_cpu_val"]:
                m["top_cpu_val"] = d
                m["top_cpu"] = cpu_id

        # ---- row 구성 ----
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        row = {"timestamp": ts}

        for node, agg in node_agg.items():
            tot = agg["total"] or 1
            row[f"node{node}_user_pct"] = round(agg["user"] / tot * 100, 2)
            row[f"node{node}_sys_pct"] = round(agg["sys"] / tot * 100, 2)
            row[f"node{node}_iowait_pct"] = round(agg["iowait"] / tot * 100, 2)
            row[f"node{node}_irq_pct"] = round(agg["irq"] / tot * 100, 2)
            row[f"node{node}_softirq_pct"] = round(agg["softirq"] / tot * 100, 2)

        for ctrl, m in nvme_metrics.items():
            row[f"{ctrl}_irq_per_s"] = round(m["delta"] / elapsed, 1)
            row[f"{ctrl}_top_cpu"] = m["top_cpu"] if m["top_cpu"] is not None else -1
            row[f"{ctrl}_top_cpu_node"] = self._cpu_to_node.get(m["top_cpu"], "-") if m["top_cpu"] is not None else "-"
            row[f"{ctrl}_active_queues"] = len(m["active_queues"])
            row[f"{ctrl}_active_cpus"] = len(m["active_cpus"])

        row["mem_available_mb"] = round(meminfo.get("MemAvailable", 0) / 1024, 1)
        row["mem_dirty_mb"] = round(meminfo.get("Dirty", 0) / 1024, 1)
        row["mem_writeback_mb"] = round(meminfo.get("Writeback", 0) / 1024, 1)
        row["swap_used_mb"] = round((meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)) / 1024, 1)
        row["loadavg_1m"] = load1

        if self._has_aer:
            aer = self._read_aer()
            for ctrl in self._aer_paths:
                v = aer.get(ctrl, {})
                row[f"{ctrl}_aer_cor"] = v.get("cor", 0)
                row[f"{ctrl}_aer_fatal"] = v.get("fatal", 0)
                row[f"{ctrl}_aer_nonfatal"] = v.get("nonfatal", 0)

        if self._net_enabled and self._net_ifaces:
            net_now = self._read_net_raw()
            prev = self._prev_net or {}
            for iface in self._net_ifaces:
                rx, tx = net_now.get(iface, (0, 0))
                prx, ptx = prev.get(iface, (rx, tx))
                drx = max(0, rx - prx) / (1024 * 1024) / elapsed
                dtx = max(0, tx - ptx) / (1024 * 1024) / elapsed
                row[f"net_{iface}_rx_mb_s"] = round(drx, 3)
                row[f"net_{iface}_tx_mb_s"] = round(dtx, 3)
            self._prev_net = net_now

        if self._has_numa_meminfo:
            numa_mem = self._read_numa_meminfo_mb()
            for node, vals in numa_mem.items():
                row[f"node{node}_mem_free_mb"] = vals.get("free_mb")
                row[f"node{node}_mem_used_mb"] = vals.get("used_mb")

        if self._has_cpufreq:
            freqs = self._read_cpu_freq_mhz()
            # cpu_id → node 매핑으로 그룹화. 노드별 평균/최대 산출.
            per_node = {n: [] for n in self._nodes}
            for cpu_id, mhz in freqs.items():
                node = self._cpu_to_node.get(cpu_id)
                if node in per_node:
                    per_node[node].append(mhz)
            for node, vals in per_node.items():
                if vals:
                    row[f"node{node}_freq_avg_mhz"] = round(sum(vals) / len(vals), 1)
                    row[f"node{node}_freq_max_mhz"] = round(max(vals), 1)
                else:
                    row[f"node{node}_freq_avg_mhz"] = None
                    row[f"node{node}_freq_max_mhz"] = None

        if self._gpu_indices:
            with self._gpu_lock:
                snap = dict(self._latest_gpu)
            for idx in self._gpu_indices:
                g = snap.get(idx, {})
                row[f"gpu{idx}_pwr_w"] = g.get("pwr")
                row[f"gpu{idx}_temp_c"] = g.get("gtemp")
                row[f"gpu{idx}_sm_pct"] = g.get("sm")
                row[f"gpu{idx}_mem_pct"] = g.get("mem")
                row[f"gpu{idx}_mem_used_mb"] = g.get("fb")
                row[f"gpu{idx}_pcie_rx_mb_s"] = g.get("rxpci")
                row[f"gpu{idx}_pcie_tx_mb_s"] = g.get("txpci")

        # Drop the first interval: the 0~1s warmup window (delta baseline only
        # just settling) is noisy and not meaningful. The first tick refreshes
        # the prev snapshots and returns, so the first *recorded* row is a
        # clean 1s~2s interval.
        if self._warmed_up:
            self._csv_writer.writerow(row)
            self._csv_file.flush()
        else:
            self._warmed_up = True

        # rotate prev
        self._prev_cpu = cpu_now
        self._prev_irq = irq_now
        self._prev_ts = now_ts


# --------------------------------------------------------------------------
# helpers
def _proc_lines(path):
    try:
        with open(path) as f:
            for line in f:
                yield line.rstrip("\n")
    except FileNotFoundError:
        return


def _irq_name(line):
    parts = line.split()
    return parts[-1] if parts else ""


def _parse_cpu_list(spec):
    """'0-9,20-29' → [0,1,...,9,20,...,29]."""
    out = []
    if not spec:
        return out
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            try:
                out.extend(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                pass
    return out
