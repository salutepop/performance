"""
세션 산출물 → Markdown 요약 리포트. HTML 리포트의 자매 도구.

표 + 핵심 숫자(총 IOPS, peak BW, latency 평균/피크, GPU peak, IRQ 분포)와
auto-extracted "Top findings" (sq/cq divergence, iowait peak, GPU 활동 등).

CLI:
  python3 -m report.md_report --session-dir <session_dir> [--session-id SID] [-o out.md]
"""

import argparse
import glob
import os
import sys
from datetime import datetime

from .html_report import _discover_session, _load_topology, _load_csv


def _col_index(header, name):
    try:
        return header.index(name)
    except ValueError:
        return -1


def _floats(rows, idx):
    """주어진 컬럼의 float 리스트 (빈 값/파싱 실패는 제외)."""
    out = []
    for row in rows:
        if idx < 0 or idx >= len(row):
            continue
        v = row[idx]
        if v in ("", None):
            continue
        try:
            out.append(float(v))
        except ValueError:
            pass
    return out


def _stats(values):
    if not values:
        return {"n": 0, "avg": None, "min": None, "max": None, "sum": None}
    return {
        "n": len(values),
        "avg": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "sum": sum(values),
    }


def _fmt(v, fmt="{:.2f}", none="-"):
    return none if v is None else fmt.format(v)


def _system_aggregates(header, rows):
    """system_metrics_*.csv → 노드별 CPU%, mem, IRQ, GPU 등 요약."""
    if not header or not rows:
        return {}
    agg = {}

    # CPU per-node user/sys/iowait
    for col in header:
        if col.startswith("node") and (col.endswith("_user_pct") or col.endswith("_sys_pct")
                                       or col.endswith("_iowait_pct") or col.endswith("_irq_pct")
                                       or col.endswith("_softirq_pct")):
            agg.setdefault("cpu", {})[col] = _stats(_floats(rows, header.index(col)))

    # NVMe IRQ per controller
    for col in header:
        if col.endswith("_irq_per_s") and col.startswith("nvme"):
            agg.setdefault("nvme_irq", {})[col] = _stats(_floats(rows, header.index(col)))

    # Memory
    for col in ("mem_available_mb", "mem_dirty_mb", "mem_writeback_mb", "swap_used_mb",
                "loadavg_1m"):
        i = _col_index(header, col)
        if i >= 0:
            agg.setdefault("mem", {})[col] = _stats(_floats(rows, i))

    # CPU freq
    for col in header:
        if col.endswith("_freq_avg_mhz") or col.endswith("_freq_max_mhz"):
            agg.setdefault("freq", {})[col] = _stats(_floats(rows, header.index(col)))

    # GPU
    for col in header:
        if col.startswith("gpu") and ("_sm_pct" in col or "_pwr_w" in col or "_temp_c" in col or "_mem_used_mb" in col):
            agg.setdefault("gpu", {})[col] = _stats(_floats(rows, header.index(col)))

    return agg


def _device_aggregates(header, rows):
    """device CSV → operation별 총 IOPS/BW, peak QD, q2d/d2c 평균."""
    if not header or not rows:
        return {}
    op_i = _col_index(header, "operation")
    iops_i = _col_index(header, "iops_interval")
    bw_i = _col_index(header, "bandwidth_mb_s_interval")
    q2d_i = _col_index(header, "q2d_avg_us_interval")
    d2c_i = _col_index(header, "d2c_avg_us_interval")
    qd_i = _col_index(header, "max_qd")
    sqcq_i = _col_index(header, "sq_cq_diff_ratio")
    if op_i < 0:
        return {}

    per_op = {}
    sqcq_all = []
    weighted_rows = {}  # op → [(iops, q2d_avg, d2c_avg)] (parallel, None 허용)
    for row in rows:
        op = row[op_i] if op_i < len(row) else "?"
        s = per_op.setdefault(op, {"iops": [], "bw": [], "q2d": [], "d2c": [], "qd": []})
        for key, idx in (("iops", iops_i), ("bw", bw_i), ("q2d", q2d_i), ("d2c", d2c_i), ("qd", qd_i)):
            if idx >= 0 and idx < len(row) and row[idx] not in ("", None):
                try:
                    s[key].append(float(row[idx]))
                except ValueError:
                    pass
        if sqcq_i >= 0 and sqcq_i < len(row) and row[sqcq_i] not in ("", None):
            try:
                sqcq_all.append(float(row[sqcq_i]))
            except ValueError:
                pass
        # 가중평균용 parallel tuple (None 허용)
        def _opt(idx):
            if idx < 0 or idx >= len(row) or row[idx] in ("", None):
                return None
            try:
                return float(row[idx])
            except ValueError:
                return None
        weighted_rows.setdefault(op, []).append((_opt(iops_i), _opt(q2d_i), _opt(d2c_i)))

    out = {"_sqcq_diff_ratio": _stats(sqcq_all)}
    for op, s in per_op.items():
        out[op] = {k: _stats(v) for k, v in s.items()}
        # q2d/d2c avg를 iops-가중평균으로 override (interval outlier 보정)
        wq, wd = _weighted_lat(weighted_rows.get(op, []))
        if wq is not None:
            out[op]["q2d"]["avg"] = wq
        if wd is not None:
            out[op]["d2c"]["avg"] = wd
    return out


def _weighted_lat(triples):
    """[(iops, q2d_avg, d2c_avg)] → (weighted_q2d, weighted_d2c). iops가 0/None인 row 무시."""
    tw_q = tw_d = 0.0
    sum_q = sum_d = 0.0
    have_q = have_d = False
    for iops, q, d in triples:
        if not iops or iops <= 0:
            continue
        if q is not None:
            sum_q += q * iops; tw_q += iops; have_q = True
        if d is not None:
            sum_d += d * iops; tw_d += iops; have_d = True
    return (
        (sum_q / tw_q) if (have_q and tw_q > 0) else None,
        (sum_d / tw_d) if (have_d and tw_d > 0) else None,
    )


def _top_findings(sys_agg, dev_aggs):
    """heuristic 기반 자동 코멘트."""
    findings = []

    # SQ/CQ divergence (averaged across devices)
    diff_rates = [da.get("_sqcq_diff_ratio", {}).get("avg") for da in dev_aggs.values()]
    diff_rates = [d for d in diff_rates if d is not None]
    if diff_rates:
        avg_diff = sum(diff_rates) / len(diff_rates)
        if avg_diff > 0.2:
            findings.append(f"**High SQ-CQ cross-CPU completion** ({avg_diff*100:.1f}% mean) - check NVMe IRQ affinity")
        elif avg_diff > 0.05:
            findings.append(f"Some SQ-CQ divergence ({avg_diff*100:.1f}% mean) - within normal range")
        else:
            findings.append(f"SQ-CQ NUMA-local OK ({avg_diff*100:.2f}% diff)")

    # iowait peak
    cpu = sys_agg.get("cpu", {})
    iowait_peaks = [s.get("max", 0) or 0 for k, s in cpu.items() if k.endswith("_iowait_pct")]
    if iowait_peaks:
        peak = max(iowait_peaks)
        if peak > 10:
            findings.append(f"**High iowait peak** ({peak:.1f}%) - block-layer stalls")
        elif peak > 2:
            findings.append(f"iowait peak {peak:.1f}% (low, direct I/O looks fine)")

    # Memory dirty / writeback
    mem = sys_agg.get("mem", {})
    dirty_peak = mem.get("mem_dirty_mb", {}).get("max")
    if dirty_peak is not None:
        if dirty_peak > 100:
            findings.append(f"**Write-back cache in use** (dirty peak {dirty_peak:.1f} MB) - buffered I/O likely")
        elif dirty_peak < 5:
            findings.append(f"dirty cache near 0 ({dirty_peak:.1f} MB) - direct I/O verified")

    # GPU activity
    gpu = sys_agg.get("gpu", {})
    sm_peak = max((s.get("max", 0) or 0) for k, s in gpu.items() if "_sm_pct" in k) if gpu else 0
    pwr_peak = max((s.get("max", 0) or 0) for k, s in gpu.items() if "_pwr_w" in k) if gpu else 0
    if gpu:
        if sm_peak > 10:
            findings.append(f"**GPU active** (SM peak {sm_peak:.0f}%, power peak {pwr_peak:.0f}W)")
        else:
            findings.append(f"GPU idle (SM peak {sm_peak:.0f}%, power {pwr_peak:.0f}W)")

    # NUMA top busy node
    user_avgs = {k: s.get("avg", 0) or 0 for k, s in cpu.items() if k.endswith("_user_pct")}
    if user_avgs:
        top_node, top_avg = max(user_avgs.items(), key=lambda x: x[1])
        node_id = top_node.replace("_user_pct", "").replace("node", "")
        findings.append(f"Busiest NUMA node: {node_id} (user avg {top_avg:.1f}%)")

    return findings


def _md_table(rows, headers, align=None):
    """Markdown 표. rows: list of list. align: per-col 'l'|'r'|'c' (기본 'r')."""
    n = len(headers)
    align = align or (["r"] * n)
    out = ["| " + " | ".join(str(h) for h in headers) + " |"]
    sep = []
    for a in align:
        sep.append({"l": ":---", "r": "---:", "c": ":---:"}.get(a, "---:"))
    out.append("| " + " | ".join(sep) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def build_report(session_dir, sid):
    topo = _load_topology(session_dir, sid)
    sys_path = os.path.join(session_dir, f"system_metrics_{sid}.csv")
    device_csvs = sorted(
        p for p in glob.glob(os.path.join(session_dir, f"*_{sid}.csv"))
        if not os.path.basename(p).startswith("system_metrics_")
    )

    sys_h, sys_r = _load_csv(sys_path)
    sys_agg = _system_aggregates(sys_h, sys_r) if sys_h else {}

    dev_aggs = {}
    for dpath in device_csvs:
        h, r = _load_csv(dpath)
        if h:
            dev_aggs[os.path.basename(dpath)] = _device_aggregates(h, r)

    lines = [
        f"# Performance Report — session {sid}",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Source: `{os.path.abspath(session_dir)}`",
        "",
    ]

    # Top findings
    findings = _top_findings(sys_agg, dev_aggs)
    if findings:
        lines.append("## Top findings")
        lines.append("")
        for f in findings:
            lines.append(f"- {f}")
        lines.append("")

    # Topology
    lines.append("## 1. Topology")
    lines.append("")
    if topo:
        nodes = topo.get("nodes", [])
        nvmes = topo.get("nvme_controllers", [])
        gpus = topo.get("gpus", [])
        lines.append(f"- NUMA nodes: {', '.join(map(str, nodes)) or '(none)'}")
        lines.append(f"- NVMe controllers: {', '.join(nvmes) or '(none)'}")
        # 상세 정보 (raw.nvme_ctrls flat 구조)
        raw = topo.get("raw") or {}
        ctrls = raw.get("nvme_ctrls") or raw.get("discovered", {}).get("nvme_ctrls") or []
        if ctrls:
            ctrl_rows = []
            for c in ctrls:
                ctrl_rows.append([
                    c.get("name", "?"),
                    (c.get("model", "?") or "?").strip(),
                    c.get("firmware_rev", "?"),
                    str(c.get("queue_count", "?")),
                    c.get("state", "?"),
                    c.get("transport", "?"),
                    c.get("numa_node", "?"),
                ])
            lines.append("")
            lines.append(_md_table(ctrl_rows,
                ["ctrl", "model", "firmware", "queue", "state", "transport", "numa"],
                ["l"] * 7))
        if gpus:
            for g in gpus:
                lines.append(f"- GPU #{g.get('index','?')}: {g.get('name','?')} (NUMA {g.get('numa_node','?')})")
        else:
            lines.append("- GPUs: (none)")
    else:
        lines.append("_no topology.json_")
    lines.append("")

    # Device I/O aggregate
    lines.append("## 2. Device I/O aggregate")
    lines.append("")
    if not dev_aggs:
        lines.append("_no device CSV_")
    for dname, da in dev_aggs.items():
        lines.append(f"### {dname}")
        lines.append("")
        sqcq = da.get("_sqcq_diff_ratio", {})
        lines.append(f"_SQ-CQ diff ratio: avg {_fmt(sqcq.get('avg'), '{:.3f}')} / max {_fmt(sqcq.get('max'), '{:.3f}')}_")
        lines.append("")
        rows = []
        for op in ("read", "write", "read_ahead", "flush", "discard"):
            if op not in da:
                continue
            s = da[op]
            iops_sum = s["iops"].get("sum", 0) or 0
            bw_peak = s["bw"].get("max")
            bw_avg = s["bw"].get("avg")
            q2d_avg = s["q2d"].get("avg")
            d2c_avg = s["d2c"].get("avg")
            qd_peak = s["qd"].get("max")
            rows.append([op, f"{iops_sum:,.0f}", _fmt(bw_peak, "{:.1f}"), _fmt(bw_avg, "{:.1f}"),
                         _fmt(q2d_avg, "{:.2f}"), _fmt(d2c_avg, "{:.2f}"), _fmt(qd_peak, "{:.0f}")])
        if rows:
            lines.append(_md_table(rows,
                ["op", "total IO", "peak BW(MB/s)", "avg BW(MB/s)", "avg Q2D(us)", "avg D2C(us)", "peak QD"],
                ["l"] + ["r"] * 6))
        lines.append("")

    # System aggregate
    lines.append("## 3. System aggregate")
    lines.append("")
    cpu = sys_agg.get("cpu", {})
    if cpu:
        lines.append("### CPU (% per NUMA node)")
        lines.append("")
        # 노드별 행: user/sys/iowait/irq/softirq avg, iowait peak
        nodes_seen = sorted({k.split("_")[0].replace("node", "") for k in cpu})
        rows = []
        for node in nodes_seen:
            u = cpu.get(f"node{node}_user_pct", {}).get("avg")
            s = cpu.get(f"node{node}_sys_pct", {}).get("avg")
            io = cpu.get(f"node{node}_iowait_pct", {}).get("avg")
            io_pk = cpu.get(f"node{node}_iowait_pct", {}).get("max")
            ir = cpu.get(f"node{node}_irq_pct", {}).get("avg")
            so = cpu.get(f"node{node}_softirq_pct", {}).get("avg")
            rows.append([f"node{node}", _fmt(u), _fmt(s), _fmt(io), _fmt(io_pk), _fmt(ir), _fmt(so)])
        lines.append(_md_table(rows,
            ["node", "user avg", "sys avg", "iowait avg", "iowait peak", "irq avg", "softirq avg"],
            ["l"] + ["r"] * 6))
        lines.append("")

    mem = sys_agg.get("mem", {})
    if mem:
        lines.append("### Memory")
        lines.append("")
        rows = [
            ["mem_available_mb", _fmt(mem.get("mem_available_mb", {}).get("avg"), "{:.0f}"),
             _fmt(mem.get("mem_available_mb", {}).get("min"), "{:.0f}")],
            ["mem_dirty_mb", _fmt(mem.get("mem_dirty_mb", {}).get("avg")),
             _fmt(mem.get("mem_dirty_mb", {}).get("max"))],
            ["mem_writeback_mb", _fmt(mem.get("mem_writeback_mb", {}).get("avg")),
             _fmt(mem.get("mem_writeback_mb", {}).get("max"))],
            ["swap_used_mb", _fmt(mem.get("swap_used_mb", {}).get("avg")),
             _fmt(mem.get("swap_used_mb", {}).get("max"))],
            ["loadavg 1m", _fmt(mem.get("loadavg_1m", {}).get("avg")),
             _fmt(mem.get("loadavg_1m", {}).get("max"))],
        ]
        lines.append(_md_table(rows, ["metric", "avg", "min/max"], ["l", "r", "r"]))
        lines.append("")

    irq = sys_agg.get("nvme_irq", {})
    if irq:
        lines.append("### NVMe IRQ rate")
        lines.append("")
        rows = []
        for col, s in irq.items():
            ctrl = col.replace("_irq_per_s", "")
            rows.append([ctrl, _fmt(s.get("avg"), "{:.0f}"), _fmt(s.get("max"), "{:.0f}")])
        lines.append(_md_table(rows, ["controller", "avg IRQ/s", "peak IRQ/s"], ["l", "r", "r"]))
        lines.append("")

    gpu = sys_agg.get("gpu", {})
    if gpu:
        lines.append("### GPU")
        lines.append("")
        # gpuN_*
        ids = sorted({k.split("_")[0] for k in gpu})
        rows = []
        for g in ids:
            sm = gpu.get(f"{g}_sm_pct", {}).get("max")
            pwr = gpu.get(f"{g}_pwr_w", {}).get("max")
            tmp = gpu.get(f"{g}_temp_c", {}).get("max")
            mb = gpu.get(f"{g}_mem_used_mb", {}).get("max")
            rows.append([g, _fmt(sm, "{:.0f}"), _fmt(pwr, "{:.0f}"), _fmt(tmp, "{:.0f}"), _fmt(mb, "{:.0f}")])
        lines.append(_md_table(rows, ["gpu", "SM peak %", "power peak W", "temp peak C", "mem peak MB"], ["l"] + ["r"] * 4))
        lines.append("")

    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Build a Markdown summary report from session artifacts")
    p.add_argument("--session-dir", default="results")
    p.add_argument("--session-id", default=None)
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    sd = args.session_dir
    if not os.path.isdir(sd):
        print(f"[!] session dir not found: {sd}", file=sys.stderr)
        return 2
    sid = args.session_id or _discover_session(sd)
    if not sid:
        print(f"[!] no topology_*.json in {sd}", file=sys.stderr)
        return 2

    out = args.output or os.path.join(sd, f"report_{sid}.md")
    md = build_report(sd, sid)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[md_report] wrote {out} ({len(md)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
