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

from .discovery import SystemDiscovery


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
        self._prev_vmstat = None  # {key: count}
        self._prev_ts = None

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
        self._prev_vmstat = self._read_vmstat_raw()
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
            cols += [f"{ctrl}_irq_per_s", f"{ctrl}_top_cpu", f"{ctrl}_top_cpu_node"]
        cols += [
            "mem_available_mb", "mem_dirty_mb", "mem_writeback_mb", "swap_used_mb",
            "pgpgin_per_s", "pgpgout_per_s", "pswpin_per_s", "pswpout_per_s",
            "loadavg_1m",
        ]
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

    def _read_vmstat_raw(self):
        out = {}
        for line in _proc_lines("/proc/vmstat"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    out[parts[0]] = int(parts[1])
                except ValueError:
                    pass
        return out

    def _read_loadavg(self):
        try:
            with open("/proc/loadavg") as f:
                return float(f.read().split()[0])
        except Exception:
            return 0.0

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
            print(f"[SysMon] nvidia-smi dmon start 실패: {e}; GPU 수집 비활성화")
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
        vm_now = self._read_vmstat_raw()
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
        nvme_metrics = {ctrl: {"delta": 0, "top_cpu": None, "top_cpu_val": -1} for ctrl in self._nvme_controllers}
        per_cpu_delta = {}  # {(ctrl, cpu_id): total_delta}
        for key, cur in irq_now.items():
            prev = self._prev_irq.get(key, 0)
            d = cur - prev
            if d < 0:
                d = 0
            ctrl, _, cpu_id = key
            per_cpu_delta[(ctrl, cpu_id)] = per_cpu_delta.get((ctrl, cpu_id), 0) + d
        for (ctrl, cpu_id), d in per_cpu_delta.items():
            m = nvme_metrics.get(ctrl)
            if m is None:
                continue
            m["delta"] += d
            if d > m["top_cpu_val"]:
                m["top_cpu_val"] = d
                m["top_cpu"] = cpu_id

        # ---- VM delta ----
        def vm_delta(k):
            return max(0, vm_now.get(k, 0) - self._prev_vmstat.get(k, 0))

        pgpgin = vm_delta("pgpgin")
        pgpgout = vm_delta("pgpgout")
        pswpin = vm_delta("pswpin")
        pswpout = vm_delta("pswpout")

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

        row["mem_available_mb"] = round(meminfo.get("MemAvailable", 0) / 1024, 1)
        row["mem_dirty_mb"] = round(meminfo.get("Dirty", 0) / 1024, 1)
        row["mem_writeback_mb"] = round(meminfo.get("Writeback", 0) / 1024, 1)
        row["swap_used_mb"] = round((meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)) / 1024, 1)

        row["pgpgin_per_s"] = round(pgpgin / elapsed, 1)
        row["pgpgout_per_s"] = round(pgpgout / elapsed, 1)
        row["pswpin_per_s"] = round(pswpin / elapsed, 1)
        row["pswpout_per_s"] = round(pswpout / elapsed, 1)
        row["loadavg_1m"] = load1

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

        self._csv_writer.writerow(row)
        self._csv_file.flush()

        # rotate prev
        self._prev_cpu = cpu_now
        self._prev_irq = irq_now
        self._prev_vmstat = vm_now
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
