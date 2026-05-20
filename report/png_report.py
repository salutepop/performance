"""
정적 PNG + Markdown 리포트 — 오프라인/GUI 없는 서버 환경용.

HTML 리포트와 같은 데이터에서 차트는 matplotlib로 PNG 파일로 저장,
markdown이 ![](figs_<sid>/foo.png) 형태로 참조. CDN 의존 없음, JS 실행 없음.

CLI:
  python3 -m report.png_report --session-dir DIR [--session-id SID] [-o out.md]

산출물:
  {output_dir}/report_png_{sid}.md
  {output_dir}/figs_{sid}/*.png            # 모든 차트
"""

import argparse
import csv
import glob
import json
import os
import re
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # headless backend - no GUI required
import matplotlib.pyplot as plt
import numpy as np

from .html_report import (
    _discover_session, _load_topology, _load_csv,
    _build_device_series, _build_system_series, _build_lba_heatmap,
    _OP_COLORS, _PALETTE,
)
from .md_report import (
    _device_aggregates, _system_aggregates, _top_findings, _md_table, _fmt,
)


# 차트 공통 스타일
plt.rcParams.update({
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "legend.fontsize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "figure.dpi": 200,
    "savefig.dpi": 200,
    "savefig.bbox": "tight",
})


def _safe_filename(s):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


def _dev_name(csv_path):
    """Device CSV is `{device}_{sid}.csv` -> return the bare `{device}`.

    A block device name never contains '_' (nvme0n1, sda, ...), and the sid
    follows the first '_', so split on it. This is robust to the session-id
    format (it may carry a label like `..._monitor_debug`), unlike the old
    `_\\d{8}_\\d{6}.csv$` regex which assumed a bare timestamp suffix.
    """
    return os.path.basename(csv_path).split("_", 1)[0]


def _xtick_thin(ax, labels, max_ticks=12):
    """timestamp 레이블이 많을 때 일부만 표시."""
    n = len(labels)
    if n <= max_ticks:
        return
    step = max(1, n // max_ticks)
    ax.set_xticks(range(0, n, step))
    ax.set_xticklabels([labels[i] for i in range(0, n, step)], rotation=45)


def _place_legend(fig, ax, handles=None, labels=None, max_cols=5):
    """All charts share the same legend slot: centered below the axes, no frame.
    Reserves bottom margin so the legend never overlaps data."""
    if labels is None:
        handles, labels = ax.get_legend_handles_labels()
    if not labels:
        return
    ncol = min(max_cols, len(labels))
    nrows = (len(labels) + ncol - 1) // ncol
    # Anchored well below the x-axis label so it never collides with rotated
    # tick labels + the "time" xlabel stacked beneath the axes.
    ax.legend(handles, labels,
              loc="upper center", bbox_to_anchor=(0.5, -0.30),
              ncol=ncol, fontsize=8, frameon=False, handlelength=2.2)
    # 1 row ~ 0.30, +0.05 per extra row
    fig.subplots_adjust(bottom=0.30 + 0.05 * max(0, nrows - 1))


def _save_line(path, labels, datasets, title, ylabel, colors=None, styles=None):
    """datasets: dict {label: [values]}, None -> NaN for matplotlib gap handling.
    colors/styles: dict {label: ...}, optional."""
    fig, ax = plt.subplots(figsize=(8, 3.5))
    for i, (name, vals) in enumerate(datasets.items()):
        y = [v if v is not None else np.nan for v in vals]
        c = (colors or {}).get(name) or _PALETTE[i % len(_PALETTE)]
        s = (styles or {}).get(name) or "-"
        ax.plot(range(len(labels)), y, label=name, color=c, linestyle=s,
                linewidth=1.3, marker=".", markersize=3)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("time")
    ax.grid(True, alpha=0.3)
    _xtick_thin(ax, labels)
    _place_legend(fig, ax)
    fig.savefig(path)
    plt.close(fig)


def _save_heatmap(path, deltas, ts_labels, title):
    """LBA heatmap (timestamps × buckets)."""
    if not deltas or not any(any(row) for row in deltas):
        return False
    arr = np.array(deltas, dtype=float).T  # (buckets, timestamps)
    arr_log = np.log10(arr + 1)
    nb = arr.shape[0]
    fig_h = max(3.0, nb * 0.05)  # bucket 수에 비례
    fig, ax = plt.subplots(figsize=(10, fig_h))
    # turbo 컬러맵: blue → cyan → green → yellow → red (no white)
    im = ax.imshow(arr_log, aspect="auto", origin="lower", cmap="turbo",
                   interpolation="nearest")
    ax.set_title(title)
    ax.set_ylabel("LBA bucket")
    ax.set_xlabel("time")
    # bucket 라벨: 0 ~ nb-1 (간헐적)
    bucket_step = max(1, nb // 8)
    ax.set_yticks(range(0, nb, bucket_step))
    _xtick_thin(ax, ts_labels)
    fig.colorbar(im, ax=ax, label="log10(access count + 1)", pad=0.02, shrink=0.8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


def _build_correlation_data(sys_header, sys_rows, device_csv_paths):
    """correlation chart 데이터: IOPS(Kiops), BW(MB/s), iowait/sys% per timestamp."""
    if not sys_header or not sys_rows or not device_csv_paths:
        return None
    try:
        ts_i = sys_header.index("timestamp")
    except ValueError:
        return None
    labels = [r[ts_i] for r in sys_rows if ts_i < len(r)]
    iowait_cols = [c for c in sys_header if c.endswith("_iowait_pct")]
    sys_cols = [c for c in sys_header if c.endswith("_sys_pct")]

    def _safe_sum(row, cols):
        s = 0.0
        for c in cols:
            i = sys_header.index(c)
            if i < len(row) and row[i] not in ("", None):
                try:
                    s += float(row[i])
                except ValueError:
                    pass
        return s
    iowait_sum = [_safe_sum(r, iowait_cols) for r in sys_rows]
    sys_sum = [_safe_sum(r, sys_cols) for r in sys_rows]

    dev_iops = {}
    dev_bw = {}
    for dpath in device_csv_paths:
        h, r = _load_csv(dpath)
        if not h:
            continue
        try:
            dts_i = h.index("timestamp")
            iops_i = h.index("iops_interval")
            bw_i = h.index("bandwidth_mb_s_interval")
        except ValueError:
            continue
        dname = _dev_name(dpath)
        iops_bucket = {}
        bw_bucket = {}
        for row in r:
            ts = row[dts_i] if dts_i < len(row) else ""
            try:
                iv = float(row[iops_i]) if iops_i < len(row) and row[iops_i] not in ("", None) else 0.0
            except ValueError:
                iv = 0.0
            try:
                bv = float(row[bw_i]) if bw_i < len(row) and row[bw_i] not in ("", None) else 0.0
            except ValueError:
                bv = 0.0
            iops_bucket[ts] = iops_bucket.get(ts, 0) + iv
            bw_bucket[ts] = bw_bucket.get(ts, 0) + bv
        dev_iops[dname] = [(iops_bucket[ts] / 1000.0) if ts in iops_bucket else None for ts in labels]
        dev_bw[dname]   = [bw_bucket.get(ts) for ts in labels]
    return {"labels": labels, "iowait": iowait_sum, "sys": sys_sum,
            "iops": dev_iops, "bw": dev_bw}


def _save_correlation_chart(path, corr, title):
    """3축 correlation chart.

    Line-style mapping (속성별 일관 규칙):
      - IOPS  : solid line + round marker      (left y, Kiops)
      - BW    : dotted line + square marker    (left y, MB/s; 디바이스 색은 IOPS와 동일)
      - CPU iowait %  : dashed (--), red       (right y)
      - CPU sys %     : dash-dot (-.), orange  (right y)

    좌측에는 IOPS와 BW 축이 함께 (BW는 IOPS 안쪽 offset 위치),
    우측에는 CPU% 단일. 디바이스 색 + 라인 스타일로 어떤 속성인지 한눈에 구분되도록.
    """
    labels = corr["labels"]
    fig, ax_iops = plt.subplots(figsize=(11, 4))
    ax_bw = ax_iops.twinx()
    ax_cpu = ax_iops.twinx()

    # BW 축을 left 안쪽으로 끌어와서 IOPS와 같은 쪽에 둠.
    # tick/label/spine 모두 좌측으로 옮기되, IOPS 라벨과 겹치지 않게 살짝 outer-left로 offset.
    ax_bw.yaxis.tick_left()
    ax_bw.yaxis.set_label_position("left")
    ax_bw.spines["left"].set_position(("axes", -0.08))
    ax_bw.spines["left"].set_visible(True)
    ax_bw.spines["right"].set_visible(False)
    ax_bw.set_frame_on(True); ax_bw.patch.set_visible(False)
    # 좌측 마진 확보 (offset BW spine이 잘리지 않도록)
    fig.subplots_adjust(left=0.12)

    palette = _PALETTE
    handles, lbls = [], []
    for i, (dn, iops) in enumerate(corr["iops"].items()):
        color = palette[i % len(palette)]
        y_iops = [v if v is not None else np.nan for v in iops]
        h1, = ax_iops.plot(range(len(labels)), y_iops, color=color, linewidth=1.4,
                           linestyle="-", marker="o", markersize=3.2,
                           label=f"IOPS {dn}")
        bw = corr["bw"].get(dn) or []
        y_bw = [v if v is not None else np.nan for v in bw]
        h2, = ax_bw.plot(range(len(labels)), y_bw, color=color, linewidth=1.2,
                         linestyle=":", marker="s", markersize=3.2,
                         label=f"BW {dn}")
        handles += [h1, h2]; lbls += [h1.get_label(), h2.get_label()]

    y_iow = [v if v is not None else np.nan for v in corr["iowait"]]
    y_sys = [v if v is not None else np.nan for v in corr["sys"]]
    h3, = ax_cpu.plot(range(len(labels)), y_iow, color="#ef4444",
                      linestyle="--", linewidth=1.3, label="iowait %")
    h4, = ax_cpu.plot(range(len(labels)), y_sys, color="#f59e0b",
                      linestyle="-.", linewidth=1.3, label="sys %")
    handles += [h3, h4]; lbls += [h3.get_label(), h4.get_label()]

    ax_iops.set_title(
        f"{title}\nstyles: IOPS = solid + circle  |  BW = dotted + square (same device color)"
        f"  |  iowait = dashed  |  sys = dash-dot",
        fontsize=9,
    )
    ax_iops.set_xlabel("time")
    ax_iops.set_ylabel("IOPS [Kiops]")
    ax_bw.set_ylabel("BW [MB/s]")
    ax_cpu.set_ylabel("% CPU")
    ax_iops.grid(True, alpha=0.3)
    _xtick_thin(ax_iops, labels)
    _place_legend(fig, ax_iops, handles=handles, labels=lbls, max_cols=3)
    fig.savefig(path)
    plt.close(fig)


def build_report(session_dir, sid):
    topo = _load_topology(session_dir, sid)
    sys_path = os.path.join(session_dir, f"system_metrics_{sid}.csv")
    sys_h, sys_r = _load_csv(sys_path)

    device_csvs = sorted(
        p for p in glob.glob(os.path.join(session_dir, f"*_{sid}.csv"))
        if not os.path.basename(p).startswith("system_metrics_")
    )

    figs_dir = os.path.join(session_dir, f"figs_{sid}")
    os.makedirs(figs_dir, exist_ok=True)

    # aggregate
    sys_agg = _system_aggregates(sys_h, sys_r) if sys_h else {}
    dev_aggs = {}
    for dp in device_csvs:
        h, r = _load_csv(dp)
        if h:
            dn = _dev_name(dp)
            dev_aggs[dn] = _device_aggregates(h, r)

    # ---- 차트 생성 ----
    fig_refs = {}  # logical_name → relative path

    # 1. I/O × System correlation (Kiops solid + BW MB/s dashed + CPU %)
    corr = _build_correlation_data(sys_h, sys_r, device_csvs)
    if corr and corr["labels"]:
        path = os.path.join(figs_dir, "correlation.png")
        _save_correlation_chart(path, corr, "I/O x System correlation")
        fig_refs["correlation"] = os.path.relpath(path, session_dir)

    # 2. Multi-device overview (IOPS, BW)
    if device_csvs and corr:
        for metric, ylabel, fname in [("iops", "ops/s", "multi_iops"),
                                       ("bw", "MB/s", "multi_bw")]:
            datasets = {}
            for dpath in device_csvs:
                h, r = _load_csv(dpath)
                if not h:
                    continue
                try:
                    ts_i = h.index("timestamp")
                    metric_i = h.index("iops_interval" if metric == "iops" else "bandwidth_mb_s_interval")
                except ValueError:
                    continue
                dn = _dev_name(dpath)
                bucket = {}
                for row in r:
                    ts = row[ts_i] if ts_i < len(row) else ""
                    try:
                        v = float(row[metric_i]) if metric_i < len(row) and row[metric_i] not in ("", None) else 0.0
                    except ValueError:
                        v = 0.0
                    bucket[ts] = bucket.get(ts, 0) + v
                datasets[dn] = [bucket.get(t) for t in corr["labels"]]
            if datasets:
                path = os.path.join(figs_dir, f"{fname}.png")
                title = f"Devices overview - {'IOPS' if metric == 'iops' else 'Bandwidth'} (op sum)"
                _save_line(path, corr["labels"], datasets, title, ylabel)
                fig_refs[fname] = os.path.relpath(path, session_dir)

    # 3. Per-device: IOPS / BW / Latency 3 차트
    for dpath in device_csvs:
        h, r = _load_csv(dpath)
        if not h:
            continue
        dname = _dev_name(dpath)
        labels, series = _build_device_series(h, r)
        if not labels or not series:
            continue
        safe = _safe_filename(dname)

        # IOPS: per-op line (matplotlib DejaVu Sans 한글 미지원 → 차트 title은 영문)
        path = os.path.join(figs_dir, f"{safe}_iops.png")
        _save_line(path, labels, {op: s["iops"] for op, s in series.items()},
                   f"{dname} - IOPS (per op)", "ops/s", colors=_OP_COLORS)
        fig_refs[f"{safe}_iops"] = os.path.relpath(path, session_dir)

        # BW: per-op line
        path = os.path.join(figs_dir, f"{safe}_bw.png")
        _save_line(path, labels, {op: s["bw"] for op, s in series.items()},
                   f"{dname} - Bandwidth (per op)", "MB/s", colors=_OP_COLORS)
        fig_refs[f"{safe}_bw"] = os.path.relpath(path, session_dir)

        # Latency: top op (avg + p50 + p99)
        has_p = any(any(v is not None for v in (s.get("p50") or []) + (s.get("p99") or []))
                    for s in series.values())
        if has_p:
            top_op = max(series.keys(),
                         key=lambda k: sum((v or 0) for v in series[k].get("iops") or []),
                         default=None)
            if top_op:
                s = series[top_op]
                path = os.path.join(figs_dir, f"{safe}_lat.png")
                _save_line(path, labels,
                           {"d2c avg": s["d2c"], "d2c p50": s["p50"], "d2c p99": s["p99"]},
                           f"{dname} — D2C latency (avg / p50 / p99, op={top_op})",
                           "us",
                           colors={"d2c avg": "#0a84ff", "d2c p50": "#30d158", "d2c p99": "#ef4444"},
                           styles={"d2c avg": "-", "d2c p50": "--", "d2c p99": "-"})
                fig_refs[f"{safe}_lat"] = os.path.relpath(path, session_dir)

        # LBA heatmap
        lba = _build_lba_heatmap(h, r)
        if lba and lba["buckets"]:
            path = os.path.join(figs_dir, f"{safe}_lba.png")
            if _save_heatmap(path, lba["buckets"], lba["timestamps"],
                             f"{dname} — LBA access heatmap (log scale)"):
                fig_refs[f"{safe}_lba"] = os.path.relpath(path, session_dir)

    # 4. System metrics (CPU per-NUMA, NVMe IRQ, Memory, GPU)
    sys_payload = _build_system_series(sys_h, sys_r) if sys_h else None
    if sys_payload:
        sys_charts = [
            ("cpu", "CPU % per NUMA node", "%"),
            ("irq", "NVMe IRQ rate", "IRQ/s"),
            ("mem", "Memory dirty/writeback", "MB"),
            ("gpu", "GPU utilization & power", "% / W"),
        ]
        for key, title, ylabel in sys_charts:
            data = sys_payload.get(key, {})
            if not data:
                continue
            path = os.path.join(figs_dir, f"sys_{key}.png")
            _save_line(path, sys_payload["labels"], data, title, ylabel)
            fig_refs[f"sys_{key}"] = os.path.relpath(path, session_dir)

    # ---- Markdown 본문 ----
    lines = [
        f"# Performance Report (static) — session {sid}",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Source: `{os.path.abspath(session_dir)}`",
        f"Figures: `{os.path.relpath(figs_dir, session_dir)}/`",
        "",
    ]

    # Top findings
    findings = _top_findings(sys_agg, dev_aggs)
    if findings:
        lines += ["## Top findings", ""]
        for f in findings:
            lines.append(f"- {f}")
        lines.append("")

    # Summary block (HTML 카드와 동일 8개 숫자)
    lines += ["## Summary", ""]
    total_read = 0.0; total_write = 0.0; peak_bw = 0.0; peak_d2c = 0.0
    sqcq_avgs = []
    for da in dev_aggs.values():
        for op in ("read", "read_ahead"):
            v = (da.get(op) or {}).get("iops", {}).get("sum")
            if v: total_read += v
        v = (da.get("write") or {}).get("iops", {}).get("sum")
        if v: total_write += v
        for op in da:
            if op.startswith("_"):
                continue
            bw = (da[op].get("bw") or {}).get("max") or 0
            if bw > peak_bw: peak_bw = bw
            d = (da[op].get("d2c") or {}).get("max") or 0
            if d > peak_d2c: peak_d2c = d
        s = (da.get("_sqcq_diff_ratio") or {}).get("avg")
        if s is not None: sqcq_avgs.append(s)
    cpu = sys_agg.get("cpu", {})
    iowait_peak = max((s.get("max", 0) or 0) for k, s in cpu.items() if k.endswith("_iowait_pct")) if cpu else 0
    sys_peak = max((s.get("max", 0) or 0) for k, s in cpu.items() if k.endswith("_sys_pct")) if cpu else 0
    gpu = sys_agg.get("gpu", {})
    gpu_pwr_peak = max((s.get("max", 0) or 0) for k, s in gpu.items() if "_pwr_w" in k) if gpu else None
    sqcq_avg = (sum(sqcq_avgs) / len(sqcq_avgs)) if sqcq_avgs else 0

    summary_rows = [
        ["Read IOPS (total)", f"{int(total_read):,}"],
        ["Write IOPS (total)", f"{int(total_write):,}"],
        ["Peak BW", f"{peak_bw:.1f} MB/s"],
        ["Max 1s-window D2C avg", f"{peak_d2c:.1f} us"],
        ["SQ↔CQ diff (mean)", f"{sqcq_avg*100:.2f} %"],
        ["CPU iowait peak", f"{iowait_peak:.1f} %"],
        ["CPU sys peak", f"{sys_peak:.1f} %"],
    ]
    if gpu_pwr_peak is not None:
        summary_rows.append(["GPU power peak", f"{gpu_pwr_peak:.0f} W"])
    lines.append(_md_table(summary_rows, ["metric", "value"], ["l", "r"]))
    lines.append("")

    # Correlation chart
    if "correlation" in fig_refs:
        lines += ["## I/O x System correlation", "",
                  f"![correlation]({fig_refs['correlation']})", ""]

    # Topology
    lines += ["## Topology", ""]
    if topo:
        nodes = topo.get("nodes", [])
        nvmes = topo.get("nvme_controllers", [])
        gpus = topo.get("gpus", [])
        lines.append(f"- NUMA nodes: {', '.join(map(str, nodes)) or '(none)'}")
        lines.append(f"- NVMe controllers: {', '.join(nvmes) or '(none)'}")
        if gpus:
            for g in gpus:
                lines.append(f"- GPU #{g.get('index','?')}: {g.get('name','?')} (NUMA {g.get('numa_node','?')})")
        # NVMe ctrl 상세
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
    lines.append("")

    # Multi-device overview
    if "multi_iops" in fig_refs or "multi_bw" in fig_refs:
        lines += ["## Devices overview", ""]
        if "multi_iops" in fig_refs:
            lines.append(f"![devices-iops]({fig_refs['multi_iops']})")
            lines.append("")
        if "multi_bw" in fig_refs:
            lines.append(f"![devices-bw]({fig_refs['multi_bw']})")
            lines.append("")

    # Per-device
    if dev_aggs:
        lines += ["## Per-device I/O", ""]
        for dname in sorted(dev_aggs.keys()):
            safe = _safe_filename(dname)
            lines += [f"### {dname}", ""]
            # aggregate 표 (op별 IOPS/BW/d2c/qd)
            da = dev_aggs[dname]
            rows = []
            for op in ("read", "write", "read_ahead", "flush", "discard"):
                if op not in da:
                    continue
                s = da[op]
                rows.append([
                    op,
                    f"{int(s['iops'].get('sum') or 0):,}",
                    _fmt(s["bw"].get("max"), "{:.1f}"),
                    _fmt(s["d2c"].get("avg"), "{:.2f}"),
                    _fmt(s["qd"].get("max"), "{:.0f}"),
                ])
            if rows:
                lines.append(_md_table(rows,
                    ["op", "total IO", "peak BW(MB/s)", "avg D2C(us)", "peak QD"],
                    ["l"] + ["r"] * 4))
                lines.append("")
            for fk, alt in [(f"{safe}_iops", "IOPS"),
                            (f"{safe}_bw", "Bandwidth"),
                            (f"{safe}_lat", "Latency"),
                            (f"{safe}_lba", "LBA heatmap")]:
                if fk in fig_refs:
                    lines.append(f"![{alt}]({fig_refs[fk]})")
                    lines.append("")

    # System metrics charts
    if sys_payload:
        lines += ["## System metrics", ""]
        for fk, alt in [("sys_cpu", "CPU per NUMA"), ("sys_irq", "NVMe IRQ"),
                         ("sys_mem", "Memory"), ("sys_gpu", "GPU")]:
            if fk in fig_refs:
                lines.append(f"![{alt}]({fig_refs[fk]})")
                lines.append("")

    return "\n".join(lines) + "\n", fig_refs


def main(argv=None):
    p = argparse.ArgumentParser(description="Static PNG + Markdown report (offline-friendly)")
    p.add_argument("--session-dir", default="results")
    p.add_argument("--session-id", default=None)
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    sd = args.session_dir
    if not os.path.isdir(sd):
        print(f"[!] session dir not found: {sd}", file=sys.stderr); return 2
    sid = args.session_id or _discover_session(sd)
    if not sid:
        print(f"[!] no topology_*.json in {sd}", file=sys.stderr); return 2

    out_path = args.output or os.path.join(sd, f"report_png_{sid}.md")
    md, refs = build_report(sd, sid)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[png_report] wrote {out_path}")
    print(f"[png_report] generated {len(refs)} figures in {os.path.dirname(next(iter(refs.values()))) if refs else '?'}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
