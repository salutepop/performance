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
            ebpf_mode=args.ebpf_mode,
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
    ebpf_mode = resolve_ebpf_mode(args.ebpf, args.ebpf_mode)

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

# debug exercises the full pipeline with the same 4-phase workload as tc00_smoke:
# seq write -> seq read -> rand write -> rand read.
_DEBUG_WORKLOADS = [
    {"name": "seq_write_128k", "rw": "write",     "bs": "128k", "iodepth": 32, "numjobs": 1, "size": "1G"},
    {"name": "seq_read_128k",  "rw": "read",      "bs": "128k", "iodepth": 32, "numjobs": 1, "size": "1G"},
    {"name": "rand_write_4k",  "rw": "randwrite", "bs": "4k",   "iodepth": 32, "numjobs": 8, "size": "1G"},
    {"name": "rand_read_4k",   "rw": "randread",  "bs": "4k",   "iodepth": 32, "numjobs": 8, "size": "1G"},
]


def cmd_debug(args):
    """Developer self-test: 4-phase fio workload + monitoring + every report.

    Creates a test file, runs seq write -> seq read -> rand write -> rand read
    (each `--duration` seconds) inside a monitored Session, renders all report
    formats, then validates artifacts. Exit code: 0 PASS / 1 FAIL.
    """
    from monitoring import Session, resolve_ebpf_mode
    from workloads.fio_runner import run_fio_job

    duration = max(1, int(args.duration))

    # Ensure the test file exists (user-owned 1 GiB, so a sudo fio run won't
    # leave a root-owned file behind).
    if not os.path.isfile(SMOKE_IMG):
        os.makedirs(os.path.dirname(SMOKE_IMG), exist_ok=True)
        with open(SMOKE_IMG, "wb") as f:
            f.truncate(1 * 1024 * 1024 * 1024)
        print(f"[debug] created test file {SMOKE_IMG} (1 GiB)")

    sys_info = _discover_sys_info()
    session_dir = _build_session_dir("debug")
    sid = os.path.basename(session_dir)
    ebpf_mode = resolve_ebpf_mode("auto", "libaio")

    print(f"[debug] session -> {session_dir}")
    print(f"[debug] config: 4 workloads x {duration}s, ebpf={ebpf_mode}, reports=all")

    fio_done = []
    with Session(session_dir, sys_info, ebpf_mode=ebpf_mode,
                 ebpf_interval=1.0, reports="all"):
        for wl in _DEBUG_WORKLOADS:
            result = run_fio_job(disk=SMOKE_IMG, workload=wl,
                                 fio_path="fio", runtime_override=duration)
            if result:
                with open(os.path.join(session_dir, f"fio_{wl['name']}.json"), "w") as f:
                    json.dump(result, f, indent=4)
                fio_done.append(wl["name"])
            else:
                print(f"[debug] workload {wl['name']} produced no result")

    # ----- artifact checks
    print("\n[debug] checking artifacts...")
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

    # every report format
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

    print()
    if failures:
        print(f"[debug] FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("[debug] PASS")
    return 0


# ---------------------------------------------------------------------------- run (compat shim)

def cmd_run(args):
    """Deprecated. Forwards to `monitor`."""
    print("[pmon] 'run' is deprecated; use 'monitor' instead.", file=sys.stderr)
    # Map old --mode to new --ebpf-mode; old run implied eBPF on.
    args.duration = None
    args.tc = None
    args.quick = False
    args.label = "adhoc"
    args.ebpf = "on"
    args.ebpf_mode = args.mode
    args.ebpf_interval = args.interval
    return cmd_monitor(args)


# ---------------------------------------------------------------------------- entry

def _add_ebpf_args(parser):
    parser.add_argument("--ebpf", choices=["auto", "on", "off"], default="auto",
                        help="eBPF I/O tracer toggle (default auto)")
    parser.add_argument("--ebpf-mode", choices=["generic", "libaio", "iouring"],
                        default="libaio", help="eBPF mode (default libaio)")
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
