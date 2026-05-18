"""
두 세션 산출물을 비교해 regression 후보를 marker로 강조하는 markdown diff 리포트.

CLI:
  python3 -m report.diff --baseline <SID> --candidate <SID> [--session-dir DIR] [-o out.md]

SID 대신 절대경로(폴더)도 받음. 같은 세션을 두 번 입력하면 모든 diff가 0%여야 함(검증).
변동 ≥ 5% (절댓값)이면 ⚠ marker, ≥ 20%면 ⛔.
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

from .html_report import _load_csv
from .md_report import _device_aggregates, _system_aggregates, _md_table


def _resolve_session(arg, session_dir):
    """arg가 절대 경로 폴더면 그대로, SID 패턴이면 session_dir/topology_{SID}.json 찾기."""
    if os.path.isdir(arg):
        # arg/ 안에 topology_*.json 있어야 함. 가장 최근 것 사용.
        topos = sorted(glob.glob(os.path.join(arg, "topology_*.json")), reverse=True)
        if not topos:
            return None, None
        m = re.search(r"topology_(\d{8}_\d{6})\.json$", topos[0])
        sid = m.group(1) if m else None
        return arg, sid
    # SID로 간주
    m = re.match(r"^(\d{8}_\d{6})$", arg)
    if m and session_dir:
        p = os.path.join(session_dir, f"topology_{arg}.json")
        if os.path.exists(p):
            return session_dir, arg
    return None, None


def _aggregate_session(sdir, sid):
    """디바이스별 _device_aggregates dict 및 system aggregate 반환."""
    dev_csvs = sorted(
        p for p in glob.glob(os.path.join(sdir, f"*_{sid}.csv"))
        if not os.path.basename(p).startswith("system_metrics_")
    )
    dev_aggs = {}
    for p in dev_csvs:
        h, r = _load_csv(p)
        if h:
            # device 이름만 키로 사용 (확장자/session_id 제거)
            base = os.path.basename(p)
            dname = re.sub(r"_\d{8}_\d{6}\.csv$", "", base)
            dev_aggs[dname] = _device_aggregates(h, r)
    sys_path = os.path.join(sdir, f"system_metrics_{sid}.csv")
    sys_h, sys_r = _load_csv(sys_path)
    sys_agg = _system_aggregates(sys_h, sys_r) if sys_h else {}
    return dev_aggs, sys_agg


def _pct_change(b, c):
    """baseline → candidate 변동률. baseline이 0이거나 None이면 None."""
    if b is None or c is None:
        return None
    try:
        b = float(b); c = float(c)
    except (TypeError, ValueError):
        return None
    if abs(b) < 1e-9:
        return None
    return (c - b) / b * 100.0


def _marker(pct):
    """변동률 → marker 문자열. None이면 빈 칸."""
    if pct is None:
        return ""
    a = abs(pct)
    if a >= 20:
        return " ⛔"
    if a >= 5:
        return " ⚠"
    return ""


def _fmt_diff(b, c, pct, fmt="{:.2f}"):
    """'baseline → candidate (+/-X%)' 형식."""
    if b is None and c is None:
        return "-"
    bs = "-" if b is None else fmt.format(b)
    cs = "-" if c is None else fmt.format(c)
    ps = "" if pct is None else f" ({pct:+.1f}%{_marker(pct)})"
    return f"{bs} → {cs}{ps}"


def _diff_device_metric(baseline_da, candidate_da, op, key, stat="avg", fmt="{:.2f}"):
    """device aggregate의 op[key][stat] 비교 → string."""
    bv = baseline_da.get(op, {}).get(key, {}).get(stat)
    cv = candidate_da.get(op, {}).get(key, {}).get(stat)
    pct = _pct_change(bv, cv)
    return _fmt_diff(bv, cv, pct, fmt)


def build_diff(base_dir, base_sid, cand_dir, cand_sid):
    base_devs, base_sys = _aggregate_session(base_dir, base_sid)
    cand_devs, cand_sys = _aggregate_session(cand_dir, cand_sid)

    lines = [
        f"# Session Diff — {base_sid} → {cand_sid}",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Baseline:  `{base_dir}` (session {base_sid})",
        f"- Candidate: `{cand_dir}` (session {cand_sid})",
        "",
        "Marker: ⚠ |Δ| ≥ 5% · ⛔ |Δ| ≥ 20% · (no marker) < 5% or n/a",
        "",
    ]

    # Device per-op diff
    all_devs = sorted(set(base_devs) | set(cand_devs))
    lines.append("## Device I/O diff (per op)")
    lines.append("")
    if not all_devs:
        lines.append("_no devices_")
    for d in all_devs:
        lines.append(f"### {d}")
        lines.append("")
        b = base_devs.get(d, {})
        c = cand_devs.get(d, {})
        ops = sorted({o for o in (set(b) | set(c)) if not o.startswith("_")})
        rows = []
        for op in ops:
            # avg BW, avg D2C(us), peak QD
            bw_d  = _diff_device_metric(b, c, op, "bw",  "avg", "{:.1f}")
            d2c_d = _diff_device_metric(b, c, op, "d2c", "avg", "{:.2f}")
            qd_d  = _diff_device_metric(b, c, op, "qd",  "max", "{:.0f}")
            iops_d = _diff_device_metric(b, c, op, "iops", "sum", "{:,.0f}")
            rows.append([op, iops_d, bw_d, d2c_d, qd_d])
        if rows:
            lines.append(_md_table(rows, ["op", "총 IO", "avg BW(MB/s)", "avg D2C(us)", "peak QD"],
                                   ["l"] + ["l"] * 4))
        else:
            lines.append("_no ops_")
        lines.append("")

    # System diff: CPU per-node + memory + IRQ + GPU
    lines.append("## System diff")
    lines.append("")
    base_cpu = base_sys.get("cpu", {})
    cand_cpu = cand_sys.get("cpu", {})
    if base_cpu or cand_cpu:
        lines.append("### CPU per-NUMA node (% avg)")
        lines.append("")
        keys = sorted(set(base_cpu) | set(cand_cpu))
        rows = []
        for k in keys:
            b = base_cpu.get(k, {}).get("avg")
            c = cand_cpu.get(k, {}).get("avg")
            rows.append([k, _fmt_diff(b, c, _pct_change(b, c))])
        lines.append(_md_table(rows, ["metric", "baseline → candidate"], ["l", "l"]))
        lines.append("")

    base_irq = base_sys.get("nvme_irq", {})
    cand_irq = cand_sys.get("nvme_irq", {})
    if base_irq or cand_irq:
        lines.append("### NVMe IRQ rate")
        lines.append("")
        keys = sorted(set(base_irq) | set(cand_irq))
        rows = []
        for k in keys:
            b = base_irq.get(k, {}).get("avg")
            c = cand_irq.get(k, {}).get("avg")
            rows.append([k.replace("_irq_per_s", ""), _fmt_diff(b, c, _pct_change(b, c), "{:.0f}")])
        lines.append(_md_table(rows, ["controller", "baseline → candidate (avg IRQ/s)"], ["l", "l"]))
        lines.append("")

    base_gpu = base_sys.get("gpu", {})
    cand_gpu = cand_sys.get("gpu", {})
    if base_gpu or cand_gpu:
        lines.append("### GPU peak")
        lines.append("")
        keys = sorted(set(base_gpu) | set(cand_gpu))
        rows = []
        for k in keys:
            b = base_gpu.get(k, {}).get("max")
            c = cand_gpu.get(k, {}).get("max")
            rows.append([k, _fmt_diff(b, c, _pct_change(b, c), "{:.0f}")])
        lines.append(_md_table(rows, ["metric", "baseline → candidate (peak)"], ["l", "l"]))
        lines.append("")

    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="두 세션을 비교해 regression 후보를 강조하는 diff 리포트")
    p.add_argument("--baseline", required=True, help="baseline SID 또는 폴더 경로")
    p.add_argument("--candidate", required=True, help="candidate SID 또는 폴더 경로")
    p.add_argument("--session-dir", default="ebpf/csv_results",
                   help="SID로 입력 시 어느 디렉터리에서 찾을지 (기본: ebpf/csv_results)")
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    base_dir, base_sid = _resolve_session(args.baseline, args.session_dir)
    cand_dir, cand_sid = _resolve_session(args.candidate, args.session_dir)
    if not base_sid:
        print(f"[!] baseline 세션 찾을 수 없음: {args.baseline}", file=sys.stderr); return 2
    if not cand_sid:
        print(f"[!] candidate 세션 찾을 수 없음: {args.candidate}", file=sys.stderr); return 2

    out = args.output or os.path.join(args.session_dir or ".", f"diff_{base_sid}_vs_{cand_sid}.md")
    md = build_diff(base_dir, base_sid, cand_dir, cand_sid)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[diff] wrote {out} ({len(md)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
