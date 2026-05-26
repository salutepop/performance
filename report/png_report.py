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
    _timestamp_window, _crop_rows,
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


def _load_phases(session_dir):
    """fio_*.json → [{name, t0, t1}] 워크로드 phase 윈도우 ("HH:MM:SS").

    job_start(epoch ms)~파일 최상위 timestamp(epoch s, fio 종료 시각)를 한
    phase로 본다. fio를 안 돌린 monitor-only 세션이면 빈 리스트."""
    phases = []
    for fp in sorted(glob.glob(os.path.join(session_dir, "fio_*.json"))):
        try:
            with open(fp) as f:
                j = json.load(f)
            jobs = j.get("jobs") or []
            js = jobs[0].get("job_start") if jobs else None
            end = j.get("timestamp")
            if js is None or end is None:
                continue
            name = (jobs[0].get("jobname")
                    or os.path.basename(fp)[len("fio_"):-len(".json")])
            phases.append({
                "name": name,
                "t0": datetime.fromtimestamp(js / 1000.0).strftime("%H:%M:%S"),
                "t1": datetime.fromtimestamp(end).strftime("%H:%M:%S"),
            })
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    phases.sort(key=lambda p: p["t0"])
    return phases


def _overlay_phases(ax, labels, phases, label=False):
    """시계열 axes에 워크로드 phase 밴드를 깐다 (x = range(len(labels))).

    인접 phase를 구분하려 한 칸 걸러 옅은 음영을 넣고, label=True면 phase
    이름을 axes 안쪽 위에 적는다."""
    if not phases or not labels:
        return
    for k, ph in enumerate(phases):
        idx = [i for i, t in enumerate(labels) if ph["t0"] <= t <= ph["t1"]]
        if not idx:
            continue
        i0, i1 = idx[0], idx[-1]
        if k % 2 == 0:
            ax.axvspan(i0 - 0.5, i1 + 0.5, color="#7f7f7f", alpha=0.10, zorder=0)
        if label:
            ax.text((i0 + i1) / 2.0, 0.97, ph["name"],
                    transform=ax.get_xaxis_transform(), ha="center", va="top",
                    fontsize=7, color="#444", clip_on=True,
                    bbox=dict(facecolor="white", edgecolor="none", alpha=0.6, pad=1))


def _save_line(path, labels, datasets, title, ylabel, colors=None, styles=None,
               phases=None):
    """datasets: dict {label: [values]}, None -> NaN for matplotlib gap handling.
    colors/styles: dict {label: ...}, optional. phases: workload bands."""
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
    _overlay_phases(ax, labels, phases, label=True)
    _xtick_thin(ax, labels)
    _place_legend(fig, ax)
    fig.savefig(path)
    plt.close(fig)


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


def _save_correlation_chart(path, corr, title, phases=None):
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
    _overlay_phases(ax_iops, labels, phases, label=True)
    _xtick_thin(ax_iops, labels)
    _place_legend(fig, ax_iops, handles=handles, labels=lbls, max_cols=4)
    fig.savefig(path)
    plt.close(fig)


# eBPF full-stack phases, in pipeline order. Unified for libaio + io_uring:
# S2Q/Q2D/D2C/C2R are common; R2U is libaio-only (io_uring reaps the CQ ring
# in userspace with no syscall, so it stays 0 and is dropped from the bar).
# The D2C disk region is split by the nvme_complete_rq tracepoint (CQ boundary)
# into D2CQ + CQ2C (block completion), both orange family. D2CQ is the device
# round-trip for local PCIe NVMe; for NVMe-oF (rdma/tcp/loop) it is the whole
# transport+remote round-trip — the per-device [transport] tag says which.
# "d2c" is the fallback single segment when nvme_complete_rq didn't fire (so no
# time is ever lost from the bar). Zero-sum segments are skipped by the chart.
_PHASE_ORDER = ["s2q", "q2d", "d2cq", "cq2c", "d2c", "c2r", "r2u"]
# Legend: pipeline number + phase abbreviation + a short description.
_PHASE_LABELS = {
    "s2q":  "1. S2Q  submit -> block queue",
    "q2d":  "2. Q2D  block queue -> dispatch",
    "d2cq": "3. D2CQ  dispatch -> device/fabric done",
    "cq2c": "4. CQ2C  device done -> block complete",
    "d2c":  "3+4. D2C  device + completion (unsplit)",
    "c2r":  "5. C2R  block complete -> engine ready (CQE/aio)",
    "r2u":  "6. R2U  engine ready -> user reap (libaio)",
}
_PHASE_COLORS = {
    "s2q": "#90caf9", "q2d": "#26a69a",
    "d2cq": "#ef6c00", "cq2c": "#ffb74d",   # D2C family
    "d2c": "#ef6c00",
    "c2r": "#ab47bc", "r2u": "#90a4ae",
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


def _dev_tag(dev, dd):
    """Device label with its auto-detected transport, e.g. 'nvme0n1 [pcie]'.

    transport is resolved by the collector at trace time (sysfs) and carried
    in ebpf_summary — it tells the reader whether D2CQ is a local device
    round-trip (pcie) or a fabric/transport round-trip (rdma/tcp/loop)."""
    tp = (dd.get("transport") or "").strip()
    return f"{dev} [{tp}]" if tp else dev


def _ebpf_rows(summary, field):
    """Flatten summary -> [(label, value), ...] over every device/op."""
    rows = []
    for dev, dd in summary.get("devices", {}).items():
        tag = _dev_tag(dev, dd)
        for op, od in dd.get("ops", {}).items():
            rows.append((f"{tag}  {op}", od.get(field)))
    return rows


def _ebpf_rows_full(summary):
    """Flatten summary -> [(label, op_dict), ...] over every device/op."""
    rows = []
    for dev, dd in summary.get("devices", {}).items():
        tag = _dev_tag(dev, dd)
        for op, od in dd.get("ops", {}).items():
            rows.append((f"{tag}  {op}", od))
    return rows


def _ebpf_phase_segments(op):
    """One device/op dict -> {phase: us}. The D2C region is split into
    D2CQ/CQ2C by the traced ratio, scaled so the segments still sum to the
    authoritative D2C total (no time is lost). Falls back to a single 'd2c'."""
    ph = op.get("phase_avg_us", {}) or {}
    seg = {"s2q": ph.get("s2q", 0) or 0, "q2d": ph.get("q2d", 0) or 0,
           "c2r": ph.get("c2r", 0) or 0, "r2u": ph.get("r2u", 0) or 0}
    d2c = ph.get("d2c", 0) or 0
    sp = op.get("d2c_split_us", {}) or {}
    sp_sum = (sp.get("d2cq", 0) or 0) + (sp.get("cq2c", 0) or 0)
    if sp_sum > 0 and d2c > 0:
        seg["d2cq"] = d2c * (sp.get("d2cq", 0) or 0) / sp_sum
        seg["cq2c"] = d2c * (sp.get("cq2c", 0) or 0) / sp_sum
    else:
        seg["d2c"] = d2c
    return seg


def _save_ebpf_latency_chart(path, summary):
    """Horizontal stacked bar — avg latency per I/O split into pipeline phases.

    Each bar is one device/op; total length = full-stack avg latency. The D2C
    disk region is sub-split into D2CQ (block_rq_issue -> nvme_complete_rq,
    device round-trip) and CQ2C (nvme_complete_rq -> block_rq_complete, block
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
                 "(D2C split into D2CQ device + CQ2C completion)")
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
            series.append((_dev_tag(dev, dd), h))
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
        ax.bar(xs, h[:nb], width=width, color=_PALETTE[i % len(_PALETTE)],
               label=dev)
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


def _build_size_timeline(header, rows, timeline=None):
    """device CSV -> (labels, read_series, write_series), each [[4k],[32k],
    [128k],[large]] per-interval counts.

    timeline: 주입된 마스터 타임라인. 주면 그 위로 reindex (결손 인터벌은 0),
    없으면 device CSV 자체 timestamp 순서를 쓴다.

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
    out = timeline if timeline is not None else labels
    read_series = [[read_ts.get(t, (0, 0, 0, 0))[b] for t in out] for b in range(4)]
    write_series = [[write_ts.get(t, (0, 0, 0, 0))[b] for t in out] for b in range(4)]
    return out, read_series, write_series


def _save_io_timeline(path, dname, labels, read_series, write_series,
                      lba_read, lba_write, phases=None):
    """One figure per device — I/O size mix and LBA access region over a shared
    time axis. Four rows (read size mix, read LBA heatmap, write size mix,
    write LBA heatmap), all sharing the x axis, so the size composition and the
    LBA region hit at each instant line up vertically.

    Size rows are non-normalized stacked areas: the eBPF interval is 1 s, so the
    stack height equals IOPS for that interval and each band is the IOPS of one
    size class. Both heatmaps share a log color scale."""
    n = len(labels)
    if n == 0:
        return False
    has_size = (any(any(s) for s in read_series)
                or any(any(s) for s in write_series))

    def _lba_arr(lba):
        if lba and lba["buckets"] and any(any(rr) for rr in lba["buckets"]):
            return np.array(lba["buckets"], dtype=float).T  # (buckets, ts)
        return None

    arr_r = _lba_arr(lba_read)
    arr_w = _lba_arr(lba_write)
    if not has_size and arr_r is None and arr_w is None:
        return False

    allmax = 0.0
    for a in (arr_r, arr_w):
        if a is not None and a.size:
            allmax = max(allmax, float(a.max()))
    vmax = np.log10(allmax + 1) if allmax > 0 else 1.0
    nb = max((a.shape[0] for a in (arr_r, arr_w) if a is not None), default=8)
    xlim = (-0.5, (n - 0.5) if n > 1 else 0.5)

    heat_h = max(1.8, nb * 0.035)
    fig = plt.figure(figsize=(12, 2 * 1.5 + 2 * heat_h + 1.4))
    gs = fig.add_gridspec(4, 2, width_ratios=[1.0, 0.018], wspace=0.03,
                          height_ratios=[1.5, heat_h, 1.5, heat_h], hspace=0.14)
    ax_sr = fig.add_subplot(gs[0, 0])
    ax_hr = fig.add_subplot(gs[1, 0], sharex=ax_sr)
    ax_sw = fig.add_subplot(gs[2, 0], sharex=ax_sr)
    ax_hw = fig.add_subplot(gs[3, 0], sharex=ax_sr)
    cax = fig.add_subplot(gs[:, 1])

    def _area(ax, series, fam):
        if any(any(s) for s in series):
            ax.stackplot(range(n), *series, colors=_SIZE_COLORS)
            ax.set_ylim(0, None)
        else:
            ax.text(0.5, 0.5, f"no {fam} I/O", ha="center", va="center",
                    transform=ax.transAxes, color="#999")
        ax.set_ylabel(f"{fam}\nIOPS", fontsize=8)

    def _heat(ax, arr, fam):
        if arr is None:
            ax.text(0.5, 0.5, f"no {fam} I/O", ha="center", va="center",
                    transform=ax.transAxes, color="#999")
            ax.set_ylabel(f"{fam} LBA", fontsize=8)
            return None
        im = ax.imshow(np.log10(arr + 1), aspect="auto", origin="lower",
                       cmap="turbo", interpolation="nearest", vmin=0, vmax=vmax,
                       extent=[xlim[0], xlim[1], 0, arr.shape[0]])
        ax.set_ylabel(f"{fam} LBA bucket\n(0=start .. N=end)", fontsize=8)
        bstep = max(1, arr.shape[0] // 6)
        ax.set_yticks(range(0, arr.shape[0] + 1, bstep))
        return im

    _area(ax_sr, read_series, "read")
    im_r = _heat(ax_hr, arr_r, "read")
    _area(ax_sw, write_series, "write")
    im_w = _heat(ax_hw, arr_w, "write")

    # phase 밴드는 stacked-area 행에만 (heatmap은 imshow가 axes를 다 채움)
    _overlay_phases(ax_sr, labels, phases, label=True)
    _overlay_phases(ax_sw, labels, phases)

    ax_sr.set_xlim(*xlim)
    for ax in (ax_sr, ax_hr, ax_sw):
        ax.tick_params(labelbottom=False)
    ax_hw.set_xlabel("time")
    _xtick_thin(ax_hw, labels)

    im = im_r if im_r is not None else im_w
    if im is not None:
        fig.colorbar(im, cax=cax, label="log10(access count + 1)")
    else:
        cax.axis("off")

    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in _SIZE_COLORS]
    fig.legend(handles, _SIZE_LABELS, loc="upper center", ncol=4,
               bbox_to_anchor=(0.5, 0.955), fontsize=8, frameon=False)
    fig.suptitle(f"{dname} — I/O size mix (IOPS) & LBA region over time "
                 f"(read top, write bottom)", y=0.995)
    fig.subplots_adjust(top=0.90, bottom=0.10, left=0.10, right=0.93)
    fig.savefig(path)
    plt.close(fig)
    return True


def _save_device_timeline(path, dname, labels, series, phases=None):
    """One figure per device — IOPS / Bandwidth / D2C / Q2D / QD over a shared
    time axis. The two latency rows use a log y scale (latency is heavy-tailed,
    so a single outlier interval would flatten a linear axis) and plot each
    active op's interval avg (solid) plus p99 (dashed). The QD row stacks the
    per-op current queue depth so the stack height = total in-flight on the
    device at interval end."""
    n = len(labels)
    if n == 0 or not series:
        return False
    x = range(n)

    def _nan(vals):
        return [v if v is not None else np.nan for v in (vals or [])]

    fig, (ax_iops, ax_bw, ax_d2c, ax_q2d, ax_qd) = plt.subplots(
        5, 1, figsize=(11, 14), sharex=True)

    # IOPS / Bandwidth — per-op lines (0 is meaningful here, so linear y)
    for op, s in series.items():
        c = _OP_COLORS.get(op)
        ax_iops.plot(x, _nan(s.get("iops")), label=op, color=c,
                     linewidth=1.3, marker=".", markersize=3)
        ax_bw.plot(x, _nan(s.get("bw")), label=op, color=c,
                   linewidth=1.3, marker=".", markersize=3)
    ax_iops.set_ylabel("IOPS\n[ops/s]", fontsize=8)
    ax_bw.set_ylabel("Bandwidth\n[MB/s]", fontsize=8)

    # latency rows: only ops that actually have D2C samples, avg + p99
    active = [op for op, s in series.items()
              if any(v is not None for v in (s.get("d2c") or []))]
    for ax, avg_key, p99_key in ((ax_d2c, "d2c", "p99"),
                                 (ax_q2d, "q2d", "q2d_p99")):
        drew = False
        for op in active:
            s = series[op]
            c = _OP_COLORS.get(op)
            avg = _nan(s.get(avg_key))
            p99 = _nan(s.get(p99_key))
            if any(not np.isnan(v) for v in avg):
                ax.plot(x, avg, color=c, linewidth=1.3, marker=".",
                        markersize=3, label=f"{op} avg")
                drew = True
            if any(not np.isnan(v) for v in p99):
                ax.plot(x, p99, color=c, linewidth=1.0, linestyle="--",
                        label=f"{op} p99")
        if drew:
            ax.set_yscale("log")
        else:
            ax.text(0.5, 0.5, "no latency samples", ha="center", va="center",
                    transform=ax.transAxes, color="#999")
    ax_d2c.set_ylabel("D2C latency\n[us · log]", fontsize=8)
    ax_q2d.set_ylabel("Q2D latency\n[us · log]", fontsize=8)

    # QD row — stacked area of per-op current_qd. The stack height shows the
    # device's total in-flight at interval end; bands show each op's share.
    # stackplot can't take NaN, so missing intervals collapse to 0.
    def _zeros(vals):
        return [(v if isinstance(v, (int, float)) else 0.0) for v in (vals or [])]

    qd_active = [op for op in series
                 if any((v or 0) > 0 for v in (series[op].get("current_qd") or []))]
    if qd_active:
        stack = [_zeros(series[op].get("current_qd")) for op in qd_active]
        colors = [_OP_COLORS.get(op, None) for op in qd_active]
        ax_qd.stackplot(x, *stack, labels=qd_active, colors=colors,
                        alpha=0.85, linewidth=0)
        # total line on top (sum of all ops) — same as stack top, but a thin
        # outline makes the peak readable when bands are similar in color.
        totals = [sum(col) for col in zip(*stack)]
        ax_qd.plot(x, totals, color="#222", linewidth=0.8, alpha=0.6,
                   label="total")
    else:
        ax_qd.text(0.5, 0.5, "no QD samples", ha="center", va="center",
                   transform=ax_qd.transAxes, color="#999")
    ax_qd.set_ylabel("Queue depth\n[in-flight]", fontsize=8)
    ax_qd.set_ylim(bottom=0)

    for ax in (ax_iops, ax_bw, ax_d2c, ax_q2d, ax_qd):
        ax.grid(True, alpha=0.3)
        ax.set_xlim(-0.5, (n - 0.5) if n > 1 else 0.5)
        ax.legend(loc="upper right", fontsize=7, ncol=2, framealpha=0.85)
        _overlay_phases(ax, labels, phases, label=(ax is ax_iops))

    ax_qd.set_xlabel("time")
    _xtick_thin(ax_qd, labels)
    fig.suptitle(f"{dname} — I/O timeline  (IOPS · Bandwidth · D2C · Q2D · QD, "
                 f"latency: solid avg / dashed p99, QD: stacked per-op current)",
                 y=0.997)
    fig.subplots_adjust(top=0.96, bottom=0.06, left=0.09, right=0.97, hspace=0.16)
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

    # SystemMonitor opens a beat before the eBPF tracer subprocess, and device
    # CSVs skip idle intervals (no I/O → row omitted) — both shift index-based
    # time-series charts out of alignment. Crop the system rows to the device
    # window, then use the cropped (gap-free, one-sample-per-interval) system
    # timestamps as the master timeline every per-device chart reindexes onto.
    timeline = None
    if sys_h and sys_r and device_csvs:
        win_t0, win_t1 = _timestamp_window(device_csvs)
        if win_t0 is not None:
            sys_r = _crop_rows(sys_h, sys_r, win_t0, win_t1)
        try:
            _ti = sys_h.index("timestamp")
            timeline = [r[_ti] for r in sys_r if _ti < len(r)] or None
        except ValueError:
            timeline = None

    # 워크로드 phase 윈도우 — 모든 시계열 차트에 밴드로 오버레이.
    phases = _load_phases(session_dir)

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
        _save_correlation_chart(path, corr, "I/O x System correlation", phases)
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
                _save_line(path, corr["labels"], datasets, title, ylabel,
                           phases=phases)
                fig_refs[fname] = os.path.relpath(path, session_dir)

    # 3. Per-device: I/O timeline (IOPS·BW·D2C·Q2D) + size-mix/LBA, 각 2장
    for dpath in device_csvs:
        h, r = _load_csv(dpath)
        if not h:
            continue
        dname = _dev_name(dpath)
        labels, series = _build_device_series(h, r, timeline)
        if not labels or not series:
            continue
        safe = _safe_filename(dname)

        # IOPS / Bandwidth / D2C / Q2D over one shared, phase-banded time axis
        path = os.path.join(figs_dir, f"{safe}_devtl.png")
        if _save_device_timeline(path, dname, labels, series, phases):
            fig_refs[f"{safe}_devtl"] = os.path.relpath(path, session_dir)

        # I/O size mix (IOPS) + LBA access region over a shared time axis
        s_labels, s_read, s_write = _build_size_timeline(h, r, timeline)
        lba_r = _build_lba_heatmap(h, r, _READ_OPS, timeline)
        lba_w = _build_lba_heatmap(h, r, _WRITE_OPS, timeline)
        path = os.path.join(figs_dir, f"{safe}_iotime.png")
        if _save_io_timeline(path, dname, s_labels, s_read, s_write,
                             lba_r, lba_w, phases):
            fig_refs[f"{safe}_iotime"] = os.path.relpath(path, session_dir)

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
            _save_line(path, sys_payload["labels"], data, title, ylabel,
                       phases=phases)
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

    # ----- funnel: context → overview → drill-down → host -----

    # Topology — 무엇을 보는지 먼저 (context)
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

    # Overview — 세션 한눈에 보기
    if "correlation" in fig_refs:
        lines += ["## I/O x System correlation", "",
                  f"![correlation]({fig_refs['correlation']})", ""]
    if "multi_iops" in fig_refs or "multi_bw" in fig_refs:
        lines += ["## Devices overview", ""]
        if "multi_iops" in fig_refs:
            lines.append(f"![devices-iops]({fig_refs['multi_iops']})")
            lines.append("")
        if "multi_bw" in fig_refs:
            lines.append(f"![devices-bw]({fig_refs['multi_bw']})")
            lines.append("")

    # Drill-down — eBPF full-stack analysis (session-wide)
    _ebpf_figs = [("ebpf_latency", "latency breakdown"), ("ebpf_size", "size dist"),
                  ("ebpf_qd", "queue depth"), ("ebpf_sqcq_matrix", "SQ-CQ matrix")]
    if any(fk in fig_refs for fk, _ in _ebpf_figs):
        lines += ["## eBPF full-stack analysis", ""]
        for fk, alt in _ebpf_figs:
            if fk in fig_refs:
                lines += [f"![{alt}]({fig_refs[fk]})", ""]

    # Drill-down — per-device
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
            for fk, alt in [(f"{safe}_devtl", "I/O timeline (IOPS·BW·D2C·Q2D·QD)"),
                            (f"{safe}_iotime", "I/O size mix & LBA region")]:
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
