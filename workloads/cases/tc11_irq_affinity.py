import os
import re


class Scenario:
    def __init__(self):
        self.tc_name = "TC11_NVMe_IRQ_Affinity_Mismatch"
        self.description = (
            "NVMe IRQ affinity CPU vs 같은 NUMA 다른 CPU vs 타 NUMA CPU에서 "
            "QD1 4K randread latency를 비교하여 IRQ mismatch 비용을 검출합니다."
        )

    # ------------------------------------------------------------------
    # 토폴로지/IRQ 매핑 helpers
    @staticmethod
    def _parse_cpu_list(s):
        """'0-3,8,10-12' → [0,1,2,3,8,10,11,12]"""
        out = []
        for token in (s or "").split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                a, b = token.split("-", 1)
                try:
                    out.extend(range(int(a), int(b) + 1))
                except ValueError:
                    continue
            else:
                try:
                    out.append(int(token))
                except ValueError:
                    continue
        return out

    @staticmethod
    def _resolve_ctrl(disk):
        """/dev/nvme0n1 / /dev/nvme0n1p1 → 'nvme0'. NVMe 아니면 None."""
        if not isinstance(disk, str) or not disk.startswith("/dev/nvme"):
            return None
        base = os.path.basename(disk)
        m = re.match(r"(nvme\d+)", base)
        return m.group(1) if m else None

    @classmethod
    def _read_irq_cpus(cls, irq):
        for fname in ("effective_affinity_list", "smp_affinity_list"):
            path = f"/proc/irq/{irq}/{fname}"
            try:
                with open(path) as f:
                    s = f.read().strip()
            except OSError:
                continue
            cpus = cls._parse_cpu_list(s)
            if cpus:
                return cpus
        return []

    @classmethod
    def _collect_irq_cpus(cls, ctrl):
        irq_dir = f"/sys/class/nvme/{ctrl}/device/msi_irqs"
        if not os.path.isdir(irq_dir):
            return set()
        all_cpus = set()
        for irq in os.listdir(irq_dir):
            all_cpus.update(cls._read_irq_cpus(irq))
        return all_cpus

    @staticmethod
    def _disk_numa(disk, sys_info):
        if not sys_info or "discovered" not in sys_info:
            return None
        for st in sys_info["discovered"].get("storage", []):
            if st.get("path") == disk:
                try:
                    return int(st.get("numa_node", -1))
                except (TypeError, ValueError):
                    return None
        return None

    @classmethod
    def _numa_cpu_map(cls, sys_info):
        out = {}
        if not sys_info or "discovered" not in sys_info:
            return out
        for node_id, info in sys_info["discovered"].get("numa", {}).items():
            try:
                nid = int(node_id)
            except (TypeError, ValueError):
                continue
            out[nid] = set(cls._parse_cpu_list(info.get("cpus", "")))
        return out

    # ------------------------------------------------------------------
    def execute(self, disk, runner_func, reporter, session_dir, numa_node, sys_info=None):
        print(f"\n[Scenario] {self.tc_name} 시작 - 대상: {disk}")

        ctrl = self._resolve_ctrl(disk)
        if not ctrl:
            print(f"  [SKIP] {disk}는 NVMe controller 매핑 불가 (file/non-nvme)")
            return

        irq_cpus = self._collect_irq_cpus(ctrl)
        if not irq_cpus:
            print(f"  [SKIP] {ctrl} IRQ affinity 정보를 읽을 수 없음 (권한/구조 차이)")
            return

        numa_map = self._numa_cpu_map(sys_info)
        disk_node = self._disk_numa(disk, sys_info)

        cases = []
        # 1) MATCH: IRQ affinity CPU 중 하나 (작은 번호부터)
        match_cpu = min(irq_cpus)
        cases.append(("match_irq", match_cpu))

        # 2) SAME_NUMA: 디스크 NUMA 내에서 IRQ에 안 묶인 CPU
        if disk_node is not None and disk_node in numa_map:
            cand = sorted(numa_map[disk_node] - irq_cpus)
            if cand:
                cases.append(("same_numa_diff_cpu", cand[0]))
            else:
                print("  [INFO] 같은 NUMA 내에 IRQ 비매칭 CPU 없음 — same_numa 케이스 skip")
        else:
            print("  [INFO] 디스크 NUMA 정보 없음 — same_numa 케이스 skip")

        # 3) CROSS_NUMA: 다른 NUMA의 임의 CPU
        cross = None
        if disk_node is not None:
            for nid, cpus in sorted(numa_map.items()):
                if nid != disk_node and cpus:
                    cross = (nid, min(cpus))
                    break
        if cross:
            cases.append((f"cross_numa(node{cross[0]})", cross[1]))
        else:
            print("  [INFO] 단일 NUMA 또는 cross-node CPU 없음 — cross_numa 케이스 skip")

        # 측정
        results = []
        for label, cpu in cases:
            wl = {
                "name": f"irq_{label}_cpu{cpu}",
                "rw": "randread",
                "bs": "4k",
                "iodepth": 1,
                "numjobs": 1,
                "runtime": 3,
                "cpus_allowed": str(cpu),
                "size": "1G",
            }
            print(f"  -> [{label}] CPU {cpu} 측정 중...")
            res = runner_func(disk, wl, numa_node=None)
            if not res:
                continue
            reporter.save_json(session_dir, f"fio_{wl['name']}.json", res)
            job = res["jobs"][0]
            lat_us = job["read"]["clat_ns"]["mean"] / 1000
            iops = job["read"]["iops"]
            results.append({"label": label, "cpu": cpu, "lat_us": lat_us, "iops": iops})
            print(f"     clat: {lat_us:8.2f} us | IOPS: {iops:9.0f}")

        if len(results) < 2:
            print("  [INFO] 비교군이 부족하여 표 출력 생략")
            return

        # 비교 표
        print("\n  " + "=" * 76)
        print(f"  ▶ [IRQ affinity 비교] {disk} (ctrl={ctrl}, disk_numa={disk_node})")
        irq_preview = sorted(irq_cpus)[:8]
        more = "…" if len(irq_cpus) > 8 else ""
        print(f"     IRQ-affinity CPUs: {irq_preview}{more} (총 {len(irq_cpus)}개)")
        print("  " + "=" * 76)
        print(f"  {'case':28} {'CPU':>4} {'clat(us)':>10} {'IOPS':>10}  {'vs match':>10}")
        print("  " + "-" * 76)
        baseline = results[0]["lat_us"]
        for r in results:
            pct = ((r["lat_us"] - baseline) / baseline * 100) if baseline > 0 else 0.0
            mark = " ⚠" if abs(pct) >= 10 else "  "
            print(
                f"  {r['label']:28} {r['cpu']:>4} {r['lat_us']:>10.2f} "
                f"{r['iops']:>10.0f}  {pct:>+8.1f}%{mark}"
            )
        print("  " + "=" * 76)
        print("  match_irq 대비 ≥10% latency 증가 시 IRQ rebalance/affinity 검토 권고")
