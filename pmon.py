#!/usr/bin/env python3
"""
pmon — performance monitoring CLI.

Monitoring is the headline; workloads are an optional input.

Subcommands:
  monitor  : observe the system (optionally while a workload runs)
  report   : render reports for an existing session
  diff     : compare two sessions
  summary  : flatten a session into a single JSON
  debug    : developer self-test for code-change verification
  run      : (deprecated alias for monitor)

Usage:
  ./pmon.py monitor --duration 30
  ./pmon.py monitor --fio "fio --name=t --filename=/tmp/x ..." --label adhoc
  ./pmon.py monitor --script monitoring/collectors/ebpf_io/src/fio.sh --ebpf on
  ./pmon.py monitor --tc tc03             # run one test case
  ./pmon.py monitor --tc all -q           # run every test case, quick mode
  ./pmon.py report                        # most recent session
  ./pmon.py diff --baseline SID1 --candidate SID2
  ./pmon.py debug                         # 4-phase fio + monitoring + all reports
"""

import argparse
import datetime
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(ROOT, "results")
LEGACY_SESSION_DIR = os.path.join(ROOT, "monitoring", "collectors", "ebpf_io", "csv_results")
SMOKE_IMG = os.path.join(ROOT, ".smoke", "smoke.img")


# ---------------------------------------------------------------------------- helpers

def _newest_session_dir():
    """Find the most recent session directory under results/, recursively.

    A session dir is one containing a topology_*.json. Falls back to
    LEGACY_SESSION_DIR if nothing found.
    """
    candidates = []
    for topo in glob.glob(os.path.join(RESULTS_DIR, "**", "topology_*.json"), recursive=True):
        candidates.append((os.path.getmtime(topo), os.path.dirname(topo)))
    if not candidates:
        return LEGACY_SESSION_DIR
    candidates.sort(reverse=True)
    return candidates[0][1]


def _discover_sys_info():
    from monitoring import SystemDiscovery
    return SystemDiscovery().discover_all()


def _build_session_dir(label):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_monitor" if not label else f"{ts}_monitor_{label}"
    path = os.path.join(RESULTS_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------- monitor

def cmd_monitor(args):
    """Run SystemMonitor (+ optional eBPF) for a window. Workload is optional."""
    # --tc delegates to the test-case runner, which manages its own sessions.
    if args.tc is not None:
        from workloads.tc_runner import run_test_cases
        return run_test_cases(
            tc_filter=args.tc,
            quick=args.quick,
            report_formats=args.report,
            ebpf_toggle=args.ebpf,
            ebpf_interval=args.ebpf_interval,
        )

    inputs = sum(bool(x) for x in (args.duration, args.fio, args.script))
    if inputs == 0:
        print("[pmon] one of --duration / --fio / --script / --tc is required",
              file=sys.stderr)
        return 2
    if inputs > 1:
        print("[pmon] --duration / --fio / --script / --tc are mutually exclusive",
              file=sys.stderr)
        return 2

    from monitoring import Session, resolve_ebpf_mode

    sys_info = _discover_sys_info()
    session_dir = _build_session_dir(args.label)
    ebpf_mode = resolve_ebpf_mode(args.ebpf)

    metadata = {
        "system": {"discovered": sys_info},
        "type": "monitor",
        "input": {
            "duration": args.duration,
            "fio": args.fio,
            "script": args.script,
        },
    }
    with open(os.path.join(session_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=4)

    print(f"[pmon] monitor start -> {session_dir} (ebpf={ebpf_mode})")

    rc = 0
    with Session(
        session_dir,
        sys_info,
        ebpf_mode=ebpf_mode,
        ebpf_interval=args.ebpf_interval,
        reports=args.report,
    ):
        if args.duration:
            try:
                time.sleep(args.duration)
            except KeyboardInterrupt:
                print("[pmon] interrupted — closing session")
        elif args.fio:
            rc = subprocess.call(args.fio, shell=True)
        elif args.script:
            rc = subprocess.call(["bash", args.script])

    print(f"[pmon] monitor done -> {session_dir} (workload rc={rc})")
    return rc


# ---------------------------------------------------------------------------- report / diff / summary

def cmd_report(args):
    session_dir = args.session_dir or _newest_session_dir()
    return 0 if _generate_reports(session_dir, args.session_id, args.format) else 2


def cmd_diff(args):
    from report.diff import main as diff_main
    session_dir = args.session_dir or _newest_session_dir()
    argv = ["--baseline", args.baseline, "--candidate", args.candidate, "--session-dir", session_dir]
    if args.output:
        argv += ["-o", args.output]
    return diff_main(argv)


def cmd_summary(args):
    from report.summary import main as summary_main
    session_dir = args.session_dir or _newest_session_dir()
    argv = ["--session-dir", session_dir]
    if args.session_id:
        argv += ["--session-id", args.session_id]
    if args.output:
        argv += ["-o", args.output]
    return summary_main(argv)


def _generate_reports(session_dir, session_id, fmt):
    """fmt: comma-separated subset of md,json,png,pdf, or 'all'/'none'."""
    if fmt == "none":
        return True
    from report.__main__ import main as report_main
    argv = ["--session-dir", session_dir, "--format", fmt]
    if session_id:
        argv += ["--session-id", session_id]
    return report_main(argv) == 0


# ---------------------------------------------------------------------------- debug

# debug exercises the full pipeline with a 4-phase fio workload, split across
# both I/O engines so engine auto-detection is self-tested:
#   seq write/read -> libaio,  rand write/read -> io_uring.
_DEBUG_WORKLOADS = [
    {"name": "seq_write_128k", "rw": "write",     "bs": "128k", "iodepth": 32, "numjobs": 1, "size": "1G", "ioengine": "libaio"},
    {"name": "seq_read_128k",  "rw": "read",      "bs": "128k", "iodepth": 32, "numjobs": 1, "size": "1G", "ioengine": "libaio"},
    {"name": "rand_write_4k",  "rw": "randwrite", "bs": "4k",   "iodepth": 32, "numjobs": 8, "size": "1G", "ioengine": "io_uring"},
    {"name": "rand_read_4k",   "rw": "randread",  "bs": "4k",   "iodepth": 32, "numjobs": 8, "size": "1G", "ioengine": "io_uring"},
]


def _mounts_overlapping(target):
    """target과 겹치는 mount들의 [(dev, mountpoint)] — raw block device 안전 가드.

    탐지: target 자체가 마운트됐거나, target이 다른 마운트된 디바이스의 부모이거나
    (e.g. /dev/nvme0n1 ⊃ /dev/nvme0n1p2), 그 반대(파티션 → 부모 디스크). nvme의
    'p<N>' 와 sd-스타일 '<N>' 파티션 명명 둘 다 처리.
    """
    if not target.startswith("/dev/"):
        return []

    def _is_part_of(child, parent):
        if not child.startswith(parent) or child == parent:
            return False
        tail = child[len(parent):]
        return tail.startswith("p") or (tail and tail[0].isdigit())

    target = os.path.realpath(target)
    hits = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2 or not parts[0].startswith("/dev/"):
                    continue
                dev = os.path.realpath(parts[0])
                if dev == target or _is_part_of(dev, target) or _is_part_of(target, dev):
                    hits.append((parts[0], parts[1]))
    except OSError:
        pass
    return hits


def _resolve_debug_target(target_arg):
    """--target 인자 해석 → (path, kind, created_file). kind: 'raw' | 'file'.

    None 이면 기본 SMOKE_IMG(1 GiB regular file, 없으면 생성). /dev/ 시작이면
    raw block device — 마운트된 디바이스면 안전상 거부. 그 외는 일반 파일로
    취급, 없으면 1 GiB로 생성. SystemExit on safety violation."""
    if not target_arg:
        if not os.path.isfile(SMOKE_IMG):
            os.makedirs(os.path.dirname(SMOKE_IMG), exist_ok=True)
            with open(SMOKE_IMG, "wb") as f:
                f.truncate(1 * 1024 * 1024 * 1024)
            print(f"[debug] created test file {SMOKE_IMG} (1 GiB)")
        return SMOKE_IMG, "file"

    if target_arg.startswith("/dev/"):
        if not os.path.exists(target_arg):
            print(f"[debug] target {target_arg} does not exist", file=sys.stderr)
            raise SystemExit(2)
        mounts = _mounts_overlapping(target_arg)
        if mounts:
            print(f"[debug] REFUSING to run on {target_arg} — it overlaps with "
                  f"mounted filesystem(s):", file=sys.stderr)
            for dev, mnt in mounts:
                print(f"        {dev} → {mnt}", file=sys.stderr)
            print(f"[debug] writing here would destroy data. Pick an unmounted "
                  f"device or unmount first.", file=sys.stderr)
            raise SystemExit(2)
        return target_arg, "raw"

    # regular file path (e.g. /mnt/test/x.img) — create if missing
    if not os.path.isfile(target_arg):
        parent = os.path.dirname(target_arg) or "."
        if not os.path.isdir(parent):
            print(f"[debug] parent dir does not exist: {parent}", file=sys.stderr)
            raise SystemExit(2)
        with open(target_arg, "wb") as f:
            f.truncate(1 * 1024 * 1024 * 1024)
        print(f"[debug] created test file {target_arg} (1 GiB)")
    return target_arg, "file"


def _target_label(path):
    """Generate session-dir suffix from a target path.

    /dev/nvme4n1   -> 'nvme4n1'
    /mnt/test/x.img -> 'x'
    .../smoke.img  -> 'smoke'
    """
    if path.startswith("/dev/"):
        return path[len("/dev/"):].replace("/", "_")
    base = os.path.basename(path) or "debug"
    return base.rsplit(".", 1)[0] if "." in base else base


def _validate_debug_artifacts(session_dir, sid, ebpf_mode, fio_done):
    """Run the artifact-existence checks for a single debug session.
    Prints OK lines, returns list of failure strings (empty = PASS)."""
    failures = []

    topo = os.path.join(session_dir, f"topology_{sid}.json")
    if not os.path.isfile(topo):
        failures.append(f"missing topology_{sid}.json")
    else:
        try:
            with open(topo) as f:
                t = json.load(f)
            if not t.get("nodes"):
                failures.append("topology has no 'nodes'")
            print(f"  [OK] topology: {len(t.get('nodes', []))} NUMA nodes, "
                  f"{len(t.get('nvme_controllers', []))} NVMe ctrls")
        except Exception as e:
            failures.append(f"topology parse error: {e}")

    csv_path = os.path.join(session_dir, f"system_metrics_{sid}.csv")
    if not os.path.isfile(csv_path):
        failures.append(f"missing system_metrics_{sid}.csv")
    else:
        with open(csv_path) as f:
            rows = sum(1 for _ in f) - 1  # minus header
        if rows < 1:
            failures.append(f"system_metrics_{sid}.csv has {rows} data rows")
        print(f"  [OK] system_metrics: {rows} rows")

    if len(fio_done) != len(_DEBUG_WORKLOADS):
        missing = [w["name"] for w in _DEBUG_WORKLOADS if w["name"] not in fio_done]
        failures.append(f"fio workloads missing output: {', '.join(missing)}")
    else:
        print(f"  [OK] fio workloads: {len(fio_done)}/4 — {', '.join(fio_done)}")

    if ebpf_mode != "off":
        io_csvs = [
            p for p in glob.glob(os.path.join(session_dir, f"*_{sid}.csv"))
            if not os.path.basename(p).startswith("system_metrics_")
        ]
        if not io_csvs:
            failures.append("no eBPF device CSV produced (tracer may have failed)")
        else:
            devs = [os.path.basename(p).split("_")[0] for p in io_csvs]
            print(f"  [OK] eBPF csv: {len(io_csvs)} device(s) — {', '.join(devs)}")

        analysis = os.path.join(session_dir, f"ebpf_analysis_{sid}.txt")
        if not os.path.isfile(analysis) or os.path.getsize(analysis) == 0:
            failures.append("no eBPF analysis text (full-stack breakdown missing)")
        else:
            print(f"  [OK] eBPF analysis: ebpf_analysis_{sid}.txt "
                  f"({os.path.getsize(analysis)} bytes)")

        summary_json = os.path.join(session_dir, f"ebpf_summary_{sid}.json")
        if not os.path.isfile(summary_json):
            failures.append("no ebpf_summary JSON (latency/size charts will be skipped)")
        else:
            try:
                with open(summary_json) as f:
                    summary = json.load(f)
                ndev = len(summary.get("devices", {}))
                print(f"  [OK] eBPF summary: ebpf_summary_{sid}.json ({ndev} device(s))")
                # Engine auto-detect: debug drives both libaio and io_uring
                # workloads — both must show up in the summary's engines block.
                engs = summary.get("engines", {})
                active = sorted(n for n, e in engs.items() if e.get("active"))
                if "libaio" in active and "iouring" in active:
                    print(f"  [OK] eBPF engine auto-detect: {', '.join(active)}")
                else:
                    failures.append(
                        f"eBPF engine auto-detect incomplete — observed "
                        f"{active or 'none'}, expected libaio + iouring")
            except Exception as e:
                failures.append(f"ebpf_summary JSON parse error: {e}")

    expected_reports = {
        "md": f"report_{sid}.md",
        "json": f"summary_{sid}.json",
        "png": f"report_png_{sid}.md",
        "pdf": f"report_{sid}.pdf",
    }
    found = [fmt for fmt, name in expected_reports.items()
             if os.path.isfile(os.path.join(session_dir, name))]
    missing = [fmt for fmt in expected_reports if fmt not in found]
    if missing:
        failures.append(f"reports missing: {', '.join(missing)}")
    print(f"  [{'OK' if not missing else '!!'}] reports: "
          f"{len(found)}/{len(expected_reports)} — {', '.join(found)}")
    return failures


def _read_phase_perf(session_dir):
    """fio_*.json 4종 읽어 {phase: {bw_mb, iops, p99_us}} 반환. 누락된 phase는 None."""
    out = {}
    for wl in _DEBUG_WORKLOADS:
        name = wl["name"]
        p = os.path.join(session_dir, f"fio_{name}.json")
        if not os.path.isfile(p):
            out[name] = None
            continue
        try:
            with open(p) as f:
                j = json.load(f)
            job = j["jobs"][0]
            op = "write" if "write" in name else "read"
            s = job[op]
            out[name] = {
                "bw_mb": s["bw"] / 1024.0,
                "iops": s["iops"],
                "p99_us": s["clat_ns"]["percentile"]["99.000000"] / 1000.0,
            }
        except (KeyError, ValueError, OSError):
            out[name] = None
    return out


def _print_target_comparison(runs):
    """runs: [(label, session_dir, sid)]. 4-phase x target 비교표 출력."""
    print()
    print("=" * 78)
    print(" Cross-target performance comparison")
    print("=" * 78)
    perfs = [(lbl, _read_phase_perf(sd)) for lbl, sd, _ in runs]
    lw = max(len(lbl) for lbl, _ in perfs)
    for wl in _DEBUG_WORKLOADS:
        name = wl["name"]
        print(f"\n  {name}  ({wl['rw']}, bs={wl['bs']}, qd={wl['iodepth']}"
              f"x{wl['numjobs']}, {wl['ioengine']})")
        print(f"    {'target':<{lw}}  {'BW(MB/s)':>10} {'IOPS':>10} {'p99(us)':>10}")
        for lbl, perf in perfs:
            p = perf.get(name)
            if p is None:
                print(f"    {lbl:<{lw}}  {'-':>10} {'-':>10} {'-':>10}")
            else:
                print(f"    {lbl:<{lw}}  {p['bw_mb']:>10.1f} {p['iops']:>10.0f} "
                      f"{p['p99_us']:>10.1f}")
    print()


def cmd_debug(args):
    """Developer self-test: 4-phase fio workload + monitoring + every report.

    With multiple --target args, runs the 4-phase suite once per target in
    separate sessions and prints a cross-target comparison table at the end.
    Exit code: 0 if all targets PASS, 1 otherwise.
    """
    from monitoring import Session, resolve_ebpf_mode
    from workloads.fio_runner import run_fio_job

    duration = max(1, int(args.duration))
    target_args = args.target or [None]   # None → default SMOKE_IMG
    # Resolve all targets up-front so any safety violation fails fast.
    resolved = [_resolve_debug_target(t) for t in target_args]
    multi = len(resolved) > 1

    sys_info = _discover_sys_info()
    ebpf_mode = resolve_ebpf_mode("auto")

    all_failures = []
    runs = []  # [(label, session_dir, sid)]
    for idx, (target, kind) in enumerate(resolved, 1):
        label = f"debug_{_target_label(target)}" if multi else "debug"
        session_dir = _build_session_dir(label)
        sid = os.path.basename(session_dir)

        if multi:
            print(f"\n[debug] === run {idx}/{len(resolved)}: {target} ({kind}) ===")
        print(f"[debug] session -> {session_dir}")
        print(f"[debug] target  -> {target} ({kind})")
        print(f"[debug] config: 4 workloads x {duration}s (2 libaio + 2 io_uring), "
              f"ebpf={ebpf_mode}, reports=all")

        fio_done = []
        with Session(session_dir, sys_info, ebpf_mode=ebpf_mode,
                     ebpf_interval=1.0, reports="all"):
            for wl in _DEBUG_WORKLOADS:
                result = run_fio_job(disk=target, workload=wl,
                                     fio_path="fio", runtime_override=duration)
                if result:
                    with open(os.path.join(session_dir, f"fio_{wl['name']}.json"), "w") as f:
                        json.dump(result, f, indent=4)
                    fio_done.append(wl["name"])
                else:
                    print(f"[debug] workload {wl['name']} produced no result")

        print("\n[debug] checking artifacts...")
        failures = _validate_debug_artifacts(session_dir, sid, ebpf_mode, fio_done)
        all_failures.extend(f"[{target}] {msg}" for msg in failures)
        runs.append((_target_label(target), session_dir, sid))

    if multi:
        _print_target_comparison(runs)

    print()
    if all_failures:
        print(f"[debug] FAIL — {len(all_failures)} issue(s):")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    print("[debug] PASS")
    return 0


# ---------------------------------------------------------------------------- run (compat shim)

def cmd_run(args):
    """Deprecated. Forwards to `monitor`."""
    print("[pmon] 'run' is deprecated; use 'monitor' instead.", file=sys.stderr)
    # old `run` implied eBPF on; engine is auto-detected now so --mode is ignored.
    args.duration = None
    args.tc = None
    args.quick = False
    args.label = "adhoc"
    args.ebpf = "on"
    args.ebpf_interval = args.interval
    return cmd_monitor(args)


# ---------------------------------------------------------------------------- entry

def _add_ebpf_args(parser):
    parser.add_argument("--ebpf", choices=["auto", "on", "off"], default="auto",
                        help="eBPF I/O tracer toggle (default auto). Engine "
                             "(libaio/io_uring) and transport are auto-detected.")
    parser.add_argument("--ebpf-interval", type=float, default=1.0,
                        help="eBPF CSV polling interval seconds (default 1.0)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="pmon", description="Performance Monitoring CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("monitor", help="observe the system (workload optional)")
    pm.add_argument("--duration", type=int, default=None,
                    help="seconds to observe with no workload")
    pm.add_argument("--fio", default=None,
                    help="fio command line (quoted)")
    pm.add_argument("--script", default=None,
                    help="path to a shell script that issues the workload")
    pm.add_argument("--tc", default=None,
                    help="run test case(s) from workloads/cases/: a name substring "
                         "(e.g. tc03) or 'all'. Mutually exclusive with --duration/--fio/--script")
    pm.add_argument("-q", "--quick", action="store_true",
                    help="quick mode: force every TC workload to ~1s (with --tc)")
    pm.add_argument("--label", default=None,
                    help="label appended to the session directory name")
    pm.add_argument("--report", default="all",
                    help="report formats to render: md,json,png,pdf comma-list or 'all'/'none' (default all)")
    _add_ebpf_args(pm)
    pm.set_defaults(func=cmd_monitor)

    pr = sub.add_parser("report", help="render reports for an existing session")
    pr.add_argument("--session-dir", default=None,
                    help="session directory (default: most recent under results/)")
    pr.add_argument("--session-id", default=None)
    pr.add_argument("--format", default="all")
    pr.set_defaults(func=cmd_report)

    pd = sub.add_parser("diff", help="compare two sessions")
    pd.add_argument("--baseline", required=True)
    pd.add_argument("--candidate", required=True)
    pd.add_argument("--session-dir", default=None)
    pd.add_argument("-o", "--output", default=None)
    pd.set_defaults(func=cmd_diff)

    ps = sub.add_parser("summary", help="flatten a session into a single JSON")
    ps.add_argument("--session-dir", default=None)
    ps.add_argument("--session-id", default=None)
    ps.add_argument("-o", "--output", default=None)
    ps.set_defaults(func=cmd_summary)

    pdbg = sub.add_parser("debug",
                          help="developer self-test: 4-phase fio + monitoring + all reports")
    pdbg.add_argument("--duration", type=int, default=3,
                      help="seconds per workload phase (default 3)")
    pdbg.add_argument("--target", nargs="+", default=None,
                      help="override fio target(s): raw block dev (/dev/nvmeXn1, "
                           "mounted devices refused) or file path. Multiple values "
                           "run the 4-phase suite once per target in separate "
                           "sessions and print a comparison table. "
                           "default: .smoke/smoke.img on root fs")
    pdbg.set_defaults(func=cmd_debug)

    # Deprecated alias for backward compatibility.
    prun = sub.add_parser("run", help="(deprecated) alias for monitor")
    prun.add_argument("--fio")
    prun.add_argument("--script")
    prun.add_argument("-m", "--mode", default="libaio",
                      choices=["generic", "libaio", "iouring"])
    prun.add_argument("-i", "--interval", type=float, default=1.0)
    prun.add_argument("--report", default="all")
    prun.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
