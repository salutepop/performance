"""
정적 PNG + Markdown 리포트 — 오프라인/GUI 없는 서버 환경용.

세션 산출물에서 차트를 matplotlib로 PNG 파일로 저장,
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

from .datasource import (
    _discover_session, _load_topology, _load_csv, _dev_name,
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


def _save_lba_split(path, lba_read, lba_write, dname):
    """LBA access heatmap, read and write side by side (timestamps x buckets).

    Both panels share a log color scale so read vs write intensity compares
    directly. A family with no I/O is skipped."""
    panels = []
    for lba, fam in [(lba_read, "read"), (lba_write, "write")]:
        if lba and lba["buckets"] and any(any(r) for r in lba["buckets"]):
            panels.append((fam, lba))
    if not panels:
        return False

    allmax = 0
    for _, lba in panels:
        for r in lba["buckets"]:
            allmax = max(allmax, max(r) if r else 0)
    vmax = np.log10(allmax + 1) if allmax > 0 else 1.0

    nb = max(len(lba["buckets"][0]) if lba["buckets"] else 0 for _, lba in panels)
    fig_h = max(3.2, nb * 0.045)
    fig, axes = plt.subplots(1, len(panels), figsize=(6.0 * len(panels), fig_h),
                             squeeze=False)
    im = None
    for ax, (fam, lba) in zip(axes[0], panels):
        arr = np.array(lba["buckets"], dtype=float).T  # (buckets, timestamps)
        im = ax.imshow(np.log10(arr + 1), aspect="auto", origin="lower",
                       cmap="turbo", interpolation="nearest", vmin=0, vmax=vmax)
        ax.set_title(fam)
        ax.set_xlabel("time")
        ax.set_ylabel("LBA bucket (0 = start of device .. N = end)")
        bstep = max(1, arr.shape[0] // 8)
        ax.set_yticks(range(0, arr.shape[0], bstep))
        _xtick_thin(ax, lba["timestamps"])
    fig.suptitle(f"{dname} — LBA access heatmap (read vs write, log scale)")
    fig.colorbar(im, ax=list(axes[0]), label="log10(access count + 1)",
                 shrink=0.85, pad=0.02)
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


# Correlation chart visual families: I/O metrics are SOLID lines, CPU metrics
# are DASHED — so at a glance "is this I/O or CPU?". Within each family, a
# distinct colour + marker tells the two series apart.
_IOPS_COLOR = "#1f77b4"   # blue
_BW_COLOR = "#2ca02c"     # green
_IOWAIT_COLOR = "#d62728" # red
_SYS_COLOR = "#ff7f0e"    # orange


def _save_correlation_chart(path, corr, title):
    """3-axis I/O x System correlation chart.

    Visual encoding:
      - IOPS  : blue,  solid line, circle marker    (left y, Kiops)
      - BW    : green, solid line, triangle marker  (left y, GB/s)
      - iowait%: red,    dashed line, x marker      (right y)
      - sys %  : orange, dashed line, + marker      (right y)

    I/O (solid) vs CPU (dashed) is the primary split; colour+marker separates
    the two series within each family. Multi-device runs shade IOPS in blues
    and BW in greens so the family is still recognisable per device.
    """
    labels = corr["labels"]
    fig, ax_iops = plt.subplots(figsize=(11, 4))
    ax_bw = ax_iops.twinx()
    ax_cpu = ax_iops.twinx()

    # BW axis sits on the inner-left, next to the IOPS axis.
    ax_bw.yaxis.tick_left()
    ax_bw.yaxis.set_label_position("left")
    ax_bw.spines["left"].set_position(("axes", -0.08))
    ax_bw.spines["left"].set_visible(True)
    ax_bw.spines["right"].set_visible(False)
    ax_bw.set_frame_on(True); ax_bw.patch.set_visible(False)
    fig.subplots_adjust(left=0.12)

    # Per-device colours: single device -> fixed strong blue/green; multiple ->
    # shades within the blue/green family so IOPS-vs-BW grouping survives.
    ndev = max(1, len(corr["iops"]))
    if ndev == 1:
        iops_cols, bw_cols = [_IOPS_COLOR], [_BW_COLOR]
    else:
        iops_cols = plt.cm.Blues(np.linspace(0.55, 0.95, ndev))
        bw_cols = plt.cm.Greens(np.linspace(0.55, 0.95, ndev))

    handles, lbls = [], []
    for i, (dn, iops) in enumerate(corr["iops"].items()):
        y_iops = [v if v is not None else np.nan for v in iops]
        h1, = ax_iops.plot(range(len(labels)), y_iops, color=iops_cols[i],
                           linewidth=1.5, linestyle="-", marker="o", markersize=3.4,
                           label=f"IOPS {dn}")
        bw = corr["bw"].get(dn) or []
        # bandwidth_mb_s_interval is MB/s -> GB/s (1 GB = 1024 MB).
        y_bw = [(v / 1024.0) if v is not None else np.nan for v in bw]
        h2, = ax_bw.plot(range(len(labels)), y_bw, color=bw_cols[i],
                         linewidth=1.5, linestyle="-", marker="^", markersize=3.4,
                         label=f"BW {dn}")
        handles += [h1, h2]; lbls += [h1.get_label(), h2.get_label()]

    y_iow = [v if v is not None else np.nan for v in corr["iowait"]]
    y_sys = [v if v is not None else np.nan for v in corr["sys"]]
    h3, = ax_cpu.plot(range(len(labels)), y_iow, color=_IOWAIT_COLOR,
                      linestyle="--", linewidth=1.3, marker="x", markersize=3.4,
                      label="iowait %")
    h4, = ax_cpu.plot(range(len(labels)), y_sys, color=_SYS_COLOR,
                      linestyle="--", linewidth=1.3, marker="+", markersize=4.0,
                      label="sys %")
    handles += [h3, h4]; lbls += [h3.get_label(), h4.get_label()]

    ax_iops.set_title(f"{title}   (I/O = solid · CPU = dashed)", fontsize=10)
    ax_iops.set_xlabel("time")
    ax_iops.set_ylabel("IOPS [Kiops]", color=_IOPS_COLOR)
    ax_bw.set_ylabel("BW [GB/s]", color=_BW_COLOR)
    ax_cpu.set_ylabel("% CPU", color=_IOWAIT_COLOR)
    ax_iops.tick_params(axis="y", colors=_IOPS_COLOR)
    ax_bw.tick_params(axis="y", colors=_BW_COLOR)
    ax_cpu.tick_params(axis="y", colors=_IOWAIT_COLOR)
    ax_iops.grid(True, alpha=0.3)
    _xtick_thin(ax_iops, labels)
    _place_legend(fig, ax_iops, handles=handles, labels=lbls, max_cols=4)
    fig.savefig(path)
    plt.close(fig)


# eBPF full-stack phases, in pipeline order. The D2C disk region is split by
# the nvme_complete_rq tracepoint into NVME (device round-trip) + BLKC (block
# completion path), both orange family. "d2c" is the fallback single segment
# when the nvme tracepoint didn't fire (so no time is ever lost from the bar).
_PHASE_ORDER = ["u2q", "q2d", "nvme", "blkc", "d2c", "c2a", "a2u"]
# Plain-language legend — numbered so it reads as the I/O pipeline order.
_PHASE_LABELS = {
    "u2q":  "1. io_submit() -> enters block queue",
    "q2d":  "2. waiting in block queue -> dispatch",
    "nvme": "3. device I/O - NVMe hardware round-trip",
    "blkc": "4. block-layer completion handling",
    "d2c":  "3+4. device + completion (D2C, unsplit)",
    "c2a":  "5. handoff to AIO layer",
    "a2u":  "6. AIO -> user wakeup (io_getevents)",
}
_PHASE_COLORS = {
    "u2q": "#90caf9", "q2d": "#26a69a",
    "nvme": "#ef6c00", "blkc": "#ffb74d",   # D2C family
    "d2c": "#ef6c00",
    "c2a": "#ab47bc", "a2u": "#90a4ae",
}
_SIZE_LABELS = ["<=4K", "4-32K", "32-128K", ">128K"]
_SIZE_COLORS = ["#08519c", "#3182bd", "#6baed6", "#bdd7e7"]


def _load_ebpf_summary(session_dir, sid):
    """ebpf_summary_<sid>.json — structured eBPF end-of-run summary, or None."""
    p = os.path.join(session_dir, f"ebpf_summary_{sid}.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _ebpf_rows(summary, field):
    """Flatten summary -> [(label, value), ...] over every device/op."""
    rows = []
    for dev, dd in summary.get("devices", {}).items():
        for op, od in dd.get("ops", {}).items():
            rows.append((f"{dev}  {op}", od.get(field)))
    return rows


def _ebpf_rows_full(summary):
    """Flatten summary -> [(label, op_dict), ...] over every device/op."""
    rows = []
    for dev, dd in summary.get("devices", {}).items():
        for op, od in dd.get("ops", {}).items():
            rows.append((f"{dev}  {op}", od))
    return rows


def _ebpf_phase_segments(op):
    """One device/op dict -> {phase: us}. The D2C region is split into
    NVME/BLKC by the traced ratio, scaled so the segments still sum to the
    authoritative D2C total (no time is lost). Falls back to a single 'd2c'."""
    ph = op.get("phase_avg_us", {}) or {}
    seg = {"u2q": ph.get("u2q", 0) or 0, "q2d": ph.get("q2d", 0) or 0,
           "c2a": ph.get("c2a", 0) or 0, "a2u": ph.get("a2u", 0) or 0}
    d2c = ph.get("d2c", 0) or 0
    sp = op.get("d2c_split_us", {}) or {}
    sp_sum = (sp.get("nvme", 0) or 0) + (sp.get("blkc", 0) or 0)
    if sp_sum > 0 and d2c > 0:
        seg["nvme"] = d2c * (sp.get("nvme", 0) or 0) / sp_sum
        seg["blkc"] = d2c * (sp.get("blkc", 0) or 0) / sp_sum
    else:
        seg["d2c"] = d2c
    return seg


def _save_ebpf_latency_chart(path, summary):
    """Horizontal stacked bar — avg latency per I/O split into pipeline phases.

    Each bar is one device/op; total length = full-stack avg latency. The D2C
    disk region is sub-split into NVME (block_rq_issue -> nvme_complete_rq,
    device round-trip) and BLKC (nvme_complete_rq -> block_rq_complete, block
    completion path), so device vs block-layer time is visible."""
    rows = []
    for lbl, op in [(l, o) for l, o in _ebpf_rows_full(summary)]:
        seg = _ebpf_phase_segments(op)
        if sum(seg.values()) > 0:
            rows.append((lbl, seg))
    if not rows:
        return False
    fig, ax = plt.subplots(figsize=(11, max(2.4, 0.62 * len(rows) + 1.8)))
    y = list(range(len(rows)))
    left = [0.0] * len(rows)
    for phase in _PHASE_ORDER:
        vals = [(r[1].get(phase) or 0) for r in rows]
        if sum(vals) <= 0:
            continue
        ax.barh(y, vals, left=left, height=0.6,
                color=_PHASE_COLORS[phase], label=_PHASE_LABELS[phase])
        left = [l + v for l, v in zip(left, vals)]
    for i, total in enumerate(left):
        ax.text(total, i, f"  {total:.1f}us", va="center", fontsize=8, color="#333")
    ax.set_yticks(y)
    ax.set_yticklabels([r[0] for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel("avg latency per I/O [us]")
    ax.set_xlim(0, (max(left) or 1) * 1.12)
    ax.set_title("eBPF full-stack latency breakdown — avg us per I/O "
                 "(D2C split into NVME device + BLKC completion)")
    ax.grid(True, axis="x", alpha=0.3)
    _place_legend(fig, ax, max_cols=4)
    fig.savefig(path)
    plt.close(fig)
    return True


def _save_ebpf_qd_chart(path, summary):
    """Bar chart — device queue-depth distribution (qd_hist, 64 buckets)."""
    series = []
    for dev, dd in summary.get("devices", {}).items():
        h = dd.get("qd_hist") or []
        if h and sum(h) > 0:
            series.append((dev, h))
    if not series:
        return False
    nb = max(len(h) for _, h in series)
    # trim trailing all-zero buckets for a tighter x-range
    last = 0
    for _, h in series:
        for b in range(len(h)):
            if h[b] > 0:
                last = max(last, b)
    nb = min(nb, last + 2)
    fig, ax = plt.subplots(figsize=(11, 3.8))
    width = 0.8 / len(series)
    for i, (dev, h) in enumerate(series):
        xs = [b + i * width for b in range(nb)]
        ax.bar(xs, h[:nb], width=width, color=_PALETTE[i % len(_PALETTE)], label=dev)
    ax.set_xlabel("device queue depth (in-flight I/O at issue; last bucket = >=63)")
    ax.set_ylabel("I/O count")
    ax.set_title("eBPF device queue-depth distribution")
    ax.grid(True, axis="y", alpha=0.3)
    _place_legend(fig, ax, max_cols=4)
    fig.savefig(path)
    plt.close(fig)
    return True


def _save_ebpf_sqcq_matrix(path, summary):
    """Heatmap — issue-CPU x complete-CPU counts. Diagonal = NUMA-local
    completion (good); off-diagonal = cross-CPU completion (IRQ affinity)."""
    matrix = summary.get("sqcq_matrix") or []
    if not matrix:
        return False
    max_cpu = 0
    for entry in matrix:
        if len(entry) >= 3:
            max_cpu = max(max_cpu, entry[0], entry[1])
    n = max_cpu + 1
    arr = np.zeros((n, n), dtype=float)
    for entry in matrix:
        if len(entry) >= 3:
            arr[entry[0], entry[1]] += entry[2]
    if arr.sum() <= 0:
        return False
    # square-ish figure; scales gracefully from a few CPUs to 384.
    side = min(11.0, max(4.0, n * 0.12))
    fig, ax = plt.subplots(figsize=(side + 1.5, side))
    im = ax.imshow(np.log10(arr + 1), origin="upper", cmap="magma",
                   aspect="equal", interpolation="nearest")
    # Reference diagonal: cells on this line are issue CPU == complete CPU
    # (the desired NUMA-local case). Anything off it is cross-CPU completion.
    ax.plot([-0.5, n - 0.5], [-0.5, n - 0.5], color="#00e5ff",
            linewidth=1.0, linestyle="--", alpha=0.55,
            label="issue CPU = complete CPU")
    ax.set_xlim(-0.5, n - 0.5)
    ax.set_ylim(n - 0.5, -0.5)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.6)
    ax.set_xlabel("complete CPU (CQ / IRQ)")
    ax.set_ylabel("issue CPU (SQ)")
    diag = float(np.trace(arr))
    total = float(arr.sum())
    ax.set_title(f"eBPF SQ x CQ CPU matrix — {n} CPUs, "
                 f"diagonal {diag/total*100:.1f}% (NUMA-local)")
    step = max(1, n // 16)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_yticks(ticks)
    fig.colorbar(im, ax=ax, label="log10(count + 1)", shrink=0.8, pad=0.02)
    fig.savefig(path)
    plt.close(fig)
    return True


_SIZE_TS_COLS = ["size_hist_4k", "size_hist_32k", "size_hist_128k", "size_hist_large"]
_READ_OPS = {"read", "read_ahead"}
_WRITE_OPS = {"write", "discard"}


def _build_size_timeline(header, rows):
    """device CSV -> (labels, read_series, write_series), each [[4k],[32k],
    [128k],[large]] per-interval counts.

    size_hist_* columns are cumulative BPF counters; delta consecutive samples
    per op, then sum the deltas per timestamp into the read vs write family."""
    try:
        ts_i = header.index("timestamp")
        op_i = header.index("operation")
        idx = [header.index(c) for c in _SIZE_TS_COLS]
    except ValueError:
        return [], [], []

    def _i(v):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    labels, seen, prev = [], set(), {}
    read_ts, write_ts = {}, {}
    for row in rows:
        ts = row[ts_i] if ts_i < len(row) else ""
        op = row[op_i] if op_i < len(row) else "?"
        cur = [_i(row[i]) if i < len(row) else 0 for i in idx]
        p = prev.get(op, [0, 0, 0, 0])
        delta = [max(0, cur[b] - p[b]) for b in range(4)]
        prev[op] = cur
        if ts not in seen:
            seen.add(ts)
            labels.append(ts)
            read_ts[ts] = [0, 0, 0, 0]
            write_ts[ts] = [0, 0, 0, 0]
        tgt = read_ts if op in _READ_OPS else (write_ts if op in _WRITE_OPS else None)
        if tgt is not None:
            for b in range(4):
                tgt[ts][b] += delta[b]
    read_series = [[read_ts[t][b] for t in labels] for b in range(4)]
    write_series = [[write_ts[t][b] for t in labels] for b in range(4)]
    return labels, read_series, write_series


def _save_size_area(path, labels, read_series, write_series, dname):
    """Two 100%-stacked areas — I/O size mix over time, read vs write split."""
    if not labels:
        return False
    has_r = any(any(s) for s in read_series)
    has_w = any(any(s) for s in write_series)
    if not has_r and not has_w:
        return False
    n = len(labels)

    def _pct(series):
        totals = [sum(series[b][i] for b in range(4)) or 1 for i in range(n)]
        return [[series[b][i] / totals[i] * 100 for i in range(n)] for b in range(4)]

    fig, (ax_r, ax_w) = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    for ax, series, fam in [(ax_r, read_series, "read (read + read_ahead)"),
                            (ax_w, write_series, "write")]:
        if any(any(s) for s in series):
            ax.stackplot(range(n), *_pct(series), labels=_SIZE_LABELS, colors=_SIZE_COLORS)
        else:
            ax.text(0.5, 0.5, f"no {fam.split()[0]} I/O", ha="center", va="center",
                    transform=ax.transAxes, color="#999")
        ax.set_xlim(0, n - 1 if n > 1 else 1)
        ax.set_ylim(0, 100)
        ax.set_ylabel(f"{fam}\nshare of I/O [%]", fontsize=8)
    ax_w.set_xlabel("time")
    _xtick_thin(ax_w, labels)
    ax_r.set_title(f"{dname} — I/O size mix over time (read vs write)")
    _place_legend(fig, ax_w, max_cols=4)
    fig.savefig(path)
    plt.close(fig)
    return True


def _save_ebpf_size_chart(path, summary):
    """100%-stacked horizontal bar — I/O size mix per device/op."""
    rows = [(lbl, sh) for lbl, sh in _ebpf_rows(summary, "size_hist")
            if sh and sum(sh) > 0]
    if not rows:
        return False
    fig, ax = plt.subplots(figsize=(11, max(2.4, 0.62 * len(rows) + 1.6)))
    y = list(range(len(rows)))
    left = [0.0] * len(rows)
    for bi, blabel in enumerate(_SIZE_LABELS):
        pcts = [(r[1][bi] / (sum(r[1]) or 1) * 100) for r in rows]
        ax.barh(y, pcts, left=left, height=0.6,
                color=_SIZE_COLORS[bi], label=blabel)
        for i, v in enumerate(pcts):
            if v >= 8:
                ax.text(left[i] + v / 2, i, f"{v:.0f}%", va="center", ha="center",
                        fontsize=7.5, color="white")
        left = [l + v for l, v in zip(left, pcts)]
    ax.set_yticks(y)
    ax.set_yticklabels([r[0] for r in rows])
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("share of I/O count [%]")
    ax.set_title("eBPF I/O size distribution — per device/op")
    _place_legend(fig, ax, max_cols=4)
    fig.savefig(path)
    plt.close(fig)
    return True


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

    # 1.5 eBPF full-stack analysis (latency breakdown / size / QD / SQ-CQ matrix)
    ebpf = _load_ebpf_summary(session_dir, sid)
    if ebpf and ebpf.get("devices"):
        for fname, fn in [("ebpf_latency", _save_ebpf_latency_chart),
                          ("ebpf_size", _save_ebpf_size_chart),
                          ("ebpf_qd", _save_ebpf_qd_chart),
                          ("ebpf_sqcq_matrix", _save_ebpf_sqcq_matrix)]:
            path = os.path.join(figs_dir, f"{fname}.png")
            if fn(path, ebpf):
                fig_refs[fname] = os.path.relpath(path, session_dir)

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

        # I/O size mix over time (read vs write, 100%-stacked area)
        s_labels, s_read, s_write = _build_size_timeline(h, r)
        if s_labels:
            path = os.path.join(figs_dir, f"{safe}_sizemix.png")
            if _save_size_area(path, s_labels, s_read, s_write, dname):
                fig_refs[f"{safe}_sizemix"] = os.path.relpath(path, session_dir)

        # LBA heatmap — read vs write split
        lba_r = _build_lba_heatmap(h, r, _READ_OPS)
        lba_w = _build_lba_heatmap(h, r, _WRITE_OPS)
        path = os.path.join(figs_dir, f"{safe}_lba.png")
        if _save_lba_split(path, lba_r, lba_w, dname):
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

    # Summary block (8개 핵심 숫자)
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

    # eBPF full-stack analysis
    _ebpf_figs = [("ebpf_latency", "latency breakdown"), ("ebpf_size", "size dist"),
                  ("ebpf_qd", "queue depth"), ("ebpf_sqcq_matrix", "SQ-CQ matrix")]
    if any(fk in fig_refs for fk, _ in _ebpf_figs):
        lines += ["## eBPF full-stack analysis", ""]
        for fk, alt in _ebpf_figs:
            if fk in fig_refs:
                lines += [f"![{alt}]({fig_refs[fk]})", ""]

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
                            (f"{safe}_sizemix", "I/O size mix"),
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
