"""
Multi-session comparison report.

기존 세션별 리포트는 그대로 두고, 여러 세션(보통 같은 워크로드를 다른
디바이스에서 돌린 결과)을 하나의 통합 리포트로 묶는다. 워크로드 phase
시작점을 0초로 정렬해 다른 wall-clock에서 돈 세션도 곡선이 겹쳐 보이게
한다.

산출물:
  results/compare_{ts}/
    compare_{ts}.md             — 표 + ASCII 차트 + 이미지 참조
    compare_{ts}.pdf            — 표지 + 차트 페이지
    figs_{ts}/                  — phase별 timeline / cross-device bar PNG
    meta.json                   — 포함된 세션 SID 목록

CLI:
  python3 -m report.compare SID1 SID2 [SID3 ...]
  python3 -m report.compare --dir results --last 2
  python3 -m report.compare --session-dirs PATH1 PATH2
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

from . import asciichart as ac
from .datasource import (_build_device_series, _dev_name, _discover_session,
                         _load_csv, _load_topology)

# debug 4-phase 정의 — pmon._DEBUG_WORKLOADS와 동기화돼 있음
# (phase_key, short_label, op_kind for fio json key)
DEBUG_PHASES = [
    ("seq_write_128k", "SW 128k", "write"),
    ("seq_read_128k",  "SR 128k", "read"),
    ("rand_write_4k",  "RW 4k",   "write"),
    ("rand_read_4k",   "RR 4k",   "read"),
]

# 디바이스 비교용 색상 팔레트 (matplotlib + ASCII에서 공유 의미는 없음, 시각 구분용)
_DEV_PALETTE = ["#0a84ff", "#ff453a", "#30d158", "#ff9f0a", "#bf5af2", "#5e5ce6"]


# ---------------------------------------------------------------------------- loading

def _session_label(session_dir):
    """`...YYYYMMDD_HHMMSS_monitor[_<label>]` 디렉터리에서 label 추출.

    cmd_debug가 multi-target일 때 라벨을 'debug_<dev>'로 붙이므로 'debug_'
    prefix는 떼서 '<dev>'만 남긴다 (Targets 표/차트 범례를 짧게 유지)."""
    base = os.path.basename(os.path.normpath(session_dir))
    parts = base.split("_")
    if "monitor" in parts:
        i = parts.index("monitor")
        tail = "_".join(parts[i + 1:])
        if tail:
            return tail[len("debug_"):] if tail.startswith("debug_") else tail
    return base


def _session_date(sid):
    """sid 'YYYYMMDD_HHMMSS_...' → date (HH:MM:SS 컬럼을 epoch로 만들 때 사용)."""
    m = re.match(r"(\d{8})_(\d{6})", sid)
    if not m:
        return datetime.date.today()
    try:
        return datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return datetime.date.today()


def _ts_to_epoch(ts_str, date):
    """'HH:MM:SS' + date → epoch sec. 파싱 실패 시 None."""
    try:
        t = datetime.datetime.strptime(ts_str, "%H:%M:%S").time()
        return datetime.datetime.combine(date, t).timestamp()
    except ValueError:
        return None


def _load_session(session_dir):
    """세션 디렉터리 → {sid, label, fio, ebpf, csvs, topology}. 실패 시 None."""
    if not os.path.isdir(session_dir):
        return None
    sid = _discover_session(session_dir)
    if not sid:
        return None

    fio = {}
    for phase_key, _, _ in DEBUG_PHASES:
        p = os.path.join(session_dir, f"fio_{phase_key}.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    fio[phase_key] = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

    ebpf = None
    p = os.path.join(session_dir, f"ebpf_summary_{sid}.json")
    if os.path.isfile(p):
        try:
            with open(p) as f:
                ebpf = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    device_csvs = sorted(
        p for p in glob.glob(os.path.join(session_dir, f"*_{sid}.csv"))
        if not os.path.basename(p).startswith("system_metrics_")
    )

    return {
        "session_dir": session_dir,
        "sid": sid,
        "label": _session_label(session_dir),
        "fio": fio,
        "ebpf": ebpf,
        "device_csvs": device_csvs,
        "topology": _load_topology(session_dir, sid),
    }


def _primary_csv(session):
    """이 세션에서 '실제로 측정하려던' 디바이스 CSV 추정.

    label이 'nvme1n1'이면 같은 컨트롤러 번호('nvme1')의 CSV 우선. 못 찾으면
    총 IOPS가 가장 높은 CSV (NVMe-oF의 nvme4c4n1 같은 형식도 커버)."""
    csvs = session.get("device_csvs") or []
    if not csvs:
        return None
    label = session.get("label", "")
    m = re.match(r"nvme(\d+)", label or "")
    if m:
        ctrl_num = m.group(1)
        for csv in csvs:
            mm = re.match(r"nvme(\d+)", _dev_name(csv))
            if mm and mm.group(1) == ctrl_num:
                return csv
    best, best_iops = None, -1.0
    for csv in csvs:
        h, r = _load_csv(csv)
        if not h:
            continue
        try:
            iops_i = h.index("iops_interval")
        except ValueError:
            continue
        total = 0.0
        for row in r:
            if iops_i < len(row) and row[iops_i]:
                try:
                    total += float(row[iops_i])
                except ValueError:
                    pass
        if total > best_iops:
            best, best_iops = csv, total
    return best


# ---------------------------------------------------------------------------- phase windows

def _phase_windows(session):
    """{phase_key: (epoch_start, epoch_end, runtime_s)} — fio job_start 기반."""
    out = {}
    for phase_key, _, _ in DEBUG_PHASES:
        j = (session.get("fio") or {}).get(phase_key)
        if not j:
            continue
        try:
            job = j["jobs"][0]
            op = "write" if "write" in phase_key else "read"
            start_ms = job.get("job_start") or 0
            runtime_ms = job[op].get("runtime") or 0
        except (KeyError, IndexError):
            continue
        if not start_ms or not runtime_ms:
            continue
        start = start_ms / 1000.0
        out[phase_key] = (start, start + runtime_ms / 1000.0, runtime_ms / 1000.0)
    return out


def _phase_perf_summary(session, phase_key):
    """fio 결과 1줄 요약 {bw_mb, iops, p99_us} (md_report와 동일 계산)."""
    j = (session.get("fio") or {}).get(phase_key)
    if not j:
        return None
    try:
        job = j["jobs"][0]
        op = "write" if "write" in phase_key else "read"
        s = job[op]
        return {
            "bw_mb": s["bw"] / 1024.0,
            "iops": s["iops"],
            "p99_us": s["clat_ns"]["percentile"]["99.000000"] / 1000.0,
        }
    except (KeyError, ValueError):
        return None


def _phase_aligned_series(session, phase_key):
    """primary CSV를 phase 윈도우로 자르고 0초 기준 rel-second 라벨로 재인덱싱.

    반환: (rel_secs[], series{op: {iops/bw/d2c/current_qd/...}})."""
    windows = _phase_windows(session)
    if phase_key not in windows:
        return [], {}
    start, end, _ = windows[phase_key]
    csv = _primary_csv(session)
    if not csv:
        return [], {}
    h, r = _load_csv(csv)
    if not h or "timestamp" not in h:
        return [], {}
    ts_i = h.index("timestamp")
    date = _session_date(session["sid"])
    in_phase = []
    for row in r:
        if ts_i >= len(row):
            continue
        ep = _ts_to_epoch(row[ts_i], date)
        # 0.5s 마진 — interval 경계가 phase boundary와 정확히 일치하지 않을 수 있음
        if ep is None or ep < start - 0.5 or ep > end + 0.5:
            continue
        in_phase.append(row)
    if not in_phase:
        return [], {}
    labels, series = _build_device_series(h, in_phase)
    rel = []
    for t in labels:
        ep = _ts_to_epoch(t, date)
        rel.append((ep - start) if ep is not None else None)
    return rel, series


# ---------------------------------------------------------------------------- markdown rendering

def _md_table(rows, headers, align=None):
    n = len(headers)
    align = align or (["r"] * n)
    out = ["| " + " | ".join(str(h) for h in headers) + " |"]
    out.append("| " + " | ".join({"l": ":---", "r": "---:", "c": ":---:"}.get(a, "---:")
                                  for a in align) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _fmt(v, fmt="{:.1f}", none="-"):
    return none if v is None else fmt.format(v)


def _targets_table(sessions):
    """디바이스 메타정보 표 (controller / model / link / numa)."""
    rows = []
    for s in sessions:
        topo = s.get("topology") or {}
        raw = topo.get("raw") or {}
        ctrls = raw.get("nvme_ctrls") or raw.get("discovered", {}).get("nvme_ctrls") or []
        # label에서 controller 번호 뽑아 매칭
        m = re.match(r"nvme(\d+)", s.get("label", ""))
        ctrl_name = f"nvme{m.group(1)}" if m else "?"
        ctrl = next((c for c in ctrls if c.get("name") == ctrl_name), None) or {}
        rows.append([
            s["label"], ctrl_name,
            (ctrl.get("model") or "?").strip(),
            ctrl.get("transport", "?"),
            str(ctrl.get("numa_node", "?")),
            s["sid"],
        ])
    return _md_table(rows, ["label", "ctrl", "model", "transport", "numa", "session id"],
                     ["l"] * 6)


def _perf_table(sessions, phase_key):
    rows = []
    for s in sessions:
        p = _phase_perf_summary(s, phase_key)
        if p is None:
            rows.append([s["label"], "-", "-", "-"])
        else:
            rows.append([s["label"],
                         f"{p['bw_mb']:.1f}",
                         f"{p['iops']:.0f}",
                         f"{p['p99_us']:.1f}"])
    return _md_table(rows, ["target", "BW(MB/s)", "IOPS", "p99(us)"],
                     ["l", "r", "r", "r"])


def _perf_hbar(sessions, phase_key, metric="bw_mb", label="BW", unit="MB/s", width=32):
    """ASCII hbar across devices for a single metric."""
    items = []
    for s in sessions:
        p = _phase_perf_summary(s, phase_key)
        items.append((s["label"], p[metric] if p else 0))
    if not any(v for _, v in items):
        return ""
    return ac.hbar(items, width=width, value_fmt="{:,.0f}", unit=f" {unit}")


def _phase_sparkline_block(sessions, phase_key, metric, unit, label_title):
    """phase-aligned per-device sparkline for one metric (sum across ops)."""
    lines = []
    for s in sessions:
        rel, series = _phase_aligned_series(s, phase_key)
        if not rel or not series:
            continue
        # sum metric across ops at each interval (ignore None)
        n = len(rel)
        vals = [0.0] * n
        any_val = False
        for op, ss in series.items():
            arr = ss.get(metric) or []
            for i in range(min(n, len(arr))):
                if isinstance(arr[i], (int, float)):
                    vals[i] += arr[i]
                    any_val = True
        if not any_val:
            continue
        spark = ac.sparkline(vals)
        lines.append((s["label"], spark, max(vals)))
    if not lines:
        return ""
    lw = max(len(lbl) for lbl, _, _ in lines)
    sw = max(len(spark) for _, spark, _ in lines)
    out = [f"**{label_title} (sparkline, phase-aligned, sum across ops)**", "", "```"]
    for lbl, spark, peak in lines:
        out.append(f"{lbl:<{lw}} │{spark:<{sw}}│ peak {peak:,.0f} {unit}")
    out.append("```")
    return "\n".join(out)


# ---------------------------------------------------------------------------- chart rendering (matplotlib)

def _try_import_mpl():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        return plt, np
    except ImportError:
        return None, None


def _save_bw_bar_chart(path, sessions, plt):
    """4-phase BW 비교 막대 차트 (디바이스 X 그룹)."""
    phase_keys = [pk for pk, _, _ in DEBUG_PHASES]
    short_labels = [sl for _, sl, _ in DEBUG_PHASES]
    n_dev = len(sessions)
    width = 0.8 / max(n_dev, 1)
    fig, ax = plt.subplots(figsize=(10, 5))
    x_base = list(range(len(phase_keys)))
    for i, s in enumerate(sessions):
        vals = []
        for pk in phase_keys:
            p = _phase_perf_summary(s, pk)
            vals.append(p["bw_mb"] if p else 0)
        xs = [x + (i - (n_dev - 1) / 2) * width for x in x_base]
        ax.bar(xs, vals, width=width * 0.95,
               color=_DEV_PALETTE[i % len(_DEV_PALETTE)], label=s["label"])
    ax.set_xticks(x_base)
    ax.set_xticklabels(short_labels)
    ax.set_ylabel("Bandwidth [MB/s]")
    ax.set_title("Cross-device bandwidth — 4 phases")
    ax.legend(loc="upper right", framealpha=0.9)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _save_iops_bar_chart(path, sessions, plt):
    phase_keys = [pk for pk, _, _ in DEBUG_PHASES]
    short_labels = [sl for _, sl, _ in DEBUG_PHASES]
    n_dev = len(sessions)
    width = 0.8 / max(n_dev, 1)
    fig, ax = plt.subplots(figsize=(10, 5))
    x_base = list(range(len(phase_keys)))
    for i, s in enumerate(sessions):
        vals = []
        for pk in phase_keys:
            p = _phase_perf_summary(s, pk)
            vals.append(p["iops"] if p else 0)
        xs = [x + (i - (n_dev - 1) / 2) * width for x in x_base]
        ax.bar(xs, vals, width=width * 0.95,
               color=_DEV_PALETTE[i % len(_DEV_PALETTE)], label=s["label"])
    ax.set_xticks(x_base)
    ax.set_xticklabels(short_labels)
    ax.set_ylabel("IOPS")
    ax.set_title("Cross-device IOPS — 4 phases")
    ax.legend(loc="upper right", framealpha=0.9)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _save_phase_timeline(path, phase_key, phase_label, sessions, plt, np):
    """phase 1개 × {IOPS, BW, total QD} 3-subplot, 디바이스별 색."""
    fig, (ax_iops, ax_bw, ax_qd) = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    have_data = False
    for i, s in enumerate(sessions):
        rel, series = _phase_aligned_series(s, phase_key)
        if not rel or not series:
            continue
        color = _DEV_PALETTE[i % len(_DEV_PALETTE)]
        # sum across ops at each interval (None → 0)
        n = len(rel)
        def _sum(metric):
            vals = [0.0] * n
            for op, ss in series.items():
                arr = ss.get(metric) or []
                for k in range(min(n, len(arr))):
                    if isinstance(arr[k], (int, float)):
                        vals[k] += arr[k]
            return vals
        x = [r if r is not None else np.nan for r in rel]
        ax_iops.plot(x, _sum("iops"), label=s["label"], color=color,
                     linewidth=1.5, marker=".", markersize=4)
        ax_bw.plot(x, _sum("bw"), label=s["label"], color=color,
                   linewidth=1.5, marker=".", markersize=4)
        ax_qd.plot(x, _sum("current_qd"), label=s["label"], color=color,
                   linewidth=1.5, marker=".", markersize=4)
        have_data = True
    for ax, ylabel in ((ax_iops, "IOPS\n[ops/s]"),
                       (ax_bw, "Bandwidth\n[MB/s]"),
                       (ax_qd, "Queue depth\n[in-flight]")):
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", fontsize=8, framealpha=0.9)
        ax.set_ylim(bottom=0)
    ax_qd.set_xlabel("seconds since phase start")
    if not have_data:
        ax_iops.text(0.5, 0.5, "no data", ha="center", va="center",
                     transform=ax_iops.transAxes, color="#999")
    fig.suptitle(f"{phase_label} — phase-aligned overlay (sum across ops)",
                 y=0.997)
    fig.subplots_adjust(top=0.94, bottom=0.08, left=0.09, right=0.97, hspace=0.18)
    fig.savefig(path)
    plt.close(fig)


def _save_pdf(pdf_path, png_paths, title, plt):
    """Cover page (제목) + 각 PNG 한 페이지씩."""
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.image import imread
    with PdfPages(pdf_path) as pdf:
        # cover
        fig = plt.figure(figsize=(11, 8.5))
        fig.text(0.5, 0.6, title, ha="center", va="center", fontsize=18, weight="bold")
        fig.text(0.5, 0.5, f"Generated {datetime.datetime.now().isoformat(timespec='seconds')}",
                 ha="center", va="center", fontsize=10, color="#555")
        pdf.savefig(fig)
        plt.close(fig)
        # image pages
        for p in png_paths:
            if not os.path.isfile(p):
                continue
            img = imread(p)
            h, w = img.shape[:2]
            ratio = w / h if h else 1.0
            fig_w = 11
            fig_h = fig_w / ratio
            if fig_h > 8.5:
                fig_h = 8.5
                fig_w = fig_h * ratio
            fig = plt.figure(figsize=(fig_w, fig_h))
            ax = fig.add_axes([0, 0, 1, 1])
            ax.imshow(img)
            ax.axis("off")
            pdf.savefig(fig)
            plt.close(fig)


# ---------------------------------------------------------------------------- orchestrator

def build_compare_report(session_dirs, out_dir=None, formats=("md", "pdf")):
    """N session 디렉터리 → 통합 비교 리포트 생성. 반환: out_dir 경로 (or None)."""
    sessions = [_load_session(sd) for sd in session_dirs]
    sessions = [s for s in sessions if s]
    if len(sessions) < 2:
        print(f"[compare] need >=2 valid sessions, got {len(sessions)}", file=sys.stderr)
        return None

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if out_dir is None:
        # 첫 세션의 부모를 results 루트로 가정
        results_root = os.path.dirname(os.path.normpath(sessions[0]["session_dir"]))
        out_dir = os.path.join(results_root, f"compare_{ts}")
    os.makedirs(out_dir, exist_ok=True)
    figs_dir = os.path.join(out_dir, f"figs_{ts}")
    os.makedirs(figs_dir, exist_ok=True)

    plt, np = _try_import_mpl()

    # ---- charts (PNG)
    png_refs = {}  # key → relpath from out_dir
    if plt:
        bw_path = os.path.join(figs_dir, "compare_bw.png")
        _save_bw_bar_chart(bw_path, sessions, plt)
        png_refs["bw"] = os.path.relpath(bw_path, out_dir)

        iops_path = os.path.join(figs_dir, "compare_iops.png")
        _save_iops_bar_chart(iops_path, sessions, plt)
        png_refs["iops"] = os.path.relpath(iops_path, out_dir)

        for phase_key, short, _ in DEBUG_PHASES:
            path = os.path.join(figs_dir, f"compare_{phase_key}.png")
            _save_phase_timeline(path, phase_key, short, sessions, plt, np)
            png_refs[phase_key] = os.path.relpath(path, out_dir)

    # ---- markdown
    lines = [
        f"# Cross-device comparison — {len(sessions)} sessions",
        "",
        f"Generated: {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Targets",
        "",
        _targets_table(sessions),
        "",
    ]

    if "bw" in png_refs:
        lines.extend(["## Cross-device summary", "",
                      f"![BW]({png_refs['bw']})", "",
                      f"![IOPS]({png_refs['iops']})", ""])

    lines.append("## Per-phase performance")
    lines.append("")
    for phase_key, short, _ in DEBUG_PHASES:
        lines.append(f"### {short}  (`{phase_key}`)")
        lines.append("")
        lines.append(_perf_table(sessions, phase_key))
        lines.append("")
        hb = _perf_hbar(sessions, phase_key)
        if hb:
            lines.extend(["**Bandwidth bar**", "", "```", hb, "```", ""])
        # phase-aligned sparklines (ASCII)
        for metric, unit, title in [
            ("iops", "IOPS", "IOPS over phase"),
            ("current_qd", "QD", "Queue depth over phase"),
        ]:
            blk = _phase_sparkline_block(sessions, phase_key, metric, unit, title)
            if blk:
                lines.append(blk)
                lines.append("")
        if phase_key in png_refs:
            lines.append(f"![{short} timeline]({png_refs[phase_key]})")
            lines.append("")

    md_path = os.path.join(out_dir, f"compare_{ts}.md")
    if "md" in formats:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"[compare] wrote {md_path} ({os.path.getsize(md_path)} bytes)")

    # ---- PDF
    if "pdf" in formats and plt:
        pdf_path = os.path.join(out_dir, f"compare_{ts}.pdf")
        title = f"Cross-device comparison — {', '.join(s['label'] for s in sessions)}"
        png_paths = [os.path.join(out_dir, png_refs[k]) for k in
                     ["bw", "iops"] + [pk for pk, _, _ in DEBUG_PHASES]
                     if k in png_refs]
        _save_pdf(pdf_path, png_paths, title, plt)
        print(f"[compare] wrote {pdf_path} ({os.path.getsize(pdf_path)} bytes)")
    elif "pdf" in formats and not plt:
        print("[compare] matplotlib not available — skipping PDF", file=sys.stderr)

    # ---- meta
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump({
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "sessions": [{"label": s["label"], "sid": s["sid"],
                          "session_dir": os.path.abspath(s["session_dir"])}
                         for s in sessions],
        }, f, indent=2)

    return out_dir


# ---------------------------------------------------------------------------- CLI

def _resolve_session_dirs(args):
    """argparse args → 세션 디렉터리 리스트."""
    root = args.dir or "results"
    if args.session_dirs:
        return [os.path.abspath(p) for p in args.session_dirs]
    if args.sessions:
        # SID로 받았으면 results 아래에서 매칭되는 디렉터리 찾기
        out = []
        for sid in args.sessions:
            # SID는 보통 디렉터리 basename 그대로 또는 일부
            matches = [d for d in glob.glob(os.path.join(root, "*"))
                       if os.path.isdir(d) and sid in os.path.basename(d)]
            if not matches:
                print(f"[compare] no session dir matched '{sid}' under {root}",
                      file=sys.stderr)
                return None
            # 가장 짧은 basename (정확한 매칭 선호)
            matches.sort(key=lambda d: len(os.path.basename(d)))
            out.append(matches[0])
        return out
    if args.last:
        candidates = []
        for d in glob.glob(os.path.join(root, "*")):
            topo = glob.glob(os.path.join(d, "topology_*.json"))
            if topo and os.path.isdir(d):
                candidates.append((os.path.getmtime(topo[0]), d))
        candidates.sort(reverse=True)
        return [d for _, d in candidates[:args.last]]
    return None


def main(argv=None):
    p = argparse.ArgumentParser(description="Build a multi-session comparison report")
    p.add_argument("sessions", nargs="*",
                   help="session IDs to match under --dir (substring OK)")
    p.add_argument("--session-dirs", nargs="+",
                   help="explicit session directory paths (overrides positional)")
    p.add_argument("--dir", default="results",
                   help="results root for --sessions/--last (default: results)")
    p.add_argument("--last", type=int, default=None,
                   help="auto-select the N most recent sessions under --dir")
    p.add_argument("-o", "--out", default=None,
                   help="output directory (default: <results>/compare_<ts>)")
    p.add_argument("--format", default="md,pdf",
                   help="comma-separated list (md, pdf). default: md,pdf")
    args = p.parse_args(argv)

    dirs = _resolve_session_dirs(args)
    if not dirs:
        print("[compare] no sessions resolved. give SID positional args, "
              "--session-dirs, or --last N", file=sys.stderr)
        return 2
    if len(dirs) < 2:
        print(f"[compare] need >=2 sessions to compare, got {len(dirs)}",
              file=sys.stderr)
        return 2

    formats = tuple(s.strip() for s in args.format.split(",") if s.strip())
    out = build_compare_report(dirs, out_dir=args.out, formats=formats)
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
