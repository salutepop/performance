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
  ./pmon.py monitor --script ebpf/fio.sh --ebpf on
  ./pmon.py report                        # most recent session
  ./pmon.py diff --baseline SID1 --candidate SID2
  ./pmon.py debug                         # quick self-test, no sudo
  ./pmon.py debug --with-fio --with-ebpf  # full E2E
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
LEGACY_SESSION_DIR = os.path.join(ROOT, "ebpf", "csv_results")
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
    from core.discovery import SystemDiscovery
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
    inputs = sum(bool(x) for x in (args.duration, args.fio, args.script))
    if inputs == 0:
        print("[pmon] one of --duration / --fio / --script is required", file=sys.stderr)
        return 2
    if inputs > 1:
        print("[pmon] --duration / --fio / --script are mutually exclusive", file=sys.stderr)
        return 2

    from core.session import Session, resolve_ebpf_mode

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
    """fmt: comma-separated subset of html,md,json,png,pdf, or 'all'/'none'."""
    if fmt == "none":
        return True
    from report.__main__ import main as report_main
    argv = ["--session-dir", session_dir, "--format", fmt]
    if session_id:
        argv += ["--session-id", session_id]
    return report_main(argv) == 0


# ---------------------------------------------------------------------------- debug

def cmd_debug(args):
    """Developer self-test. Default: 2s monitor-only smoke (no sudo)."""
    from core.session import Session, ebpf_available

    if args.full:
        args.with_ebpf = True
        args.with_fio = True

    if args.with_ebpf and not ebpf_available():
        print("[debug] --with-ebpf requested but ebpf/io_trace not built; "
              "run `cd ebpf && make`", file=sys.stderr)
        return 2
    if args.with_fio and not os.path.isfile(SMOKE_IMG):
        print(f"[debug] --with-fio requested but {SMOKE_IMG} missing; "
              f"create a 1GiB ext4 image there first", file=sys.stderr)
        return 2

    sys_info = _discover_sys_info()
    label_parts = ["debug"]
    if args.with_ebpf:
        label_parts.append("ebpf")
    if args.with_fio:
        label_parts.append("fio")
    session_dir = _build_session_dir("_".join(label_parts))

    ebpf_mode = "libaio" if args.with_ebpf else "off"
    duration = max(1, int(args.duration))

    print(f"[debug] session -> {session_dir}")
    print(f"[debug] config: duration={duration}s, ebpf={ebpf_mode}, "
          f"fio={'on' if args.with_fio else 'off'}")

    with Session(
        session_dir,
        sys_info,
        ebpf_mode=ebpf_mode,
        ebpf_interval=1.0,
        reports="md",  # minimal report to verify the pipeline
    ):
        if args.with_fio:
            fio_cmd = (
                f"sudo fio --name=debug --filename={SMOKE_IMG} "
                f"--rw=randread --bs=4k --iodepth=4 --size=64M "
                f"--runtime={duration} --time_based=1 --direct=1 --ioengine=libaio "
                f"--output-format=json --output={session_dir}/fio_debug.json "
                f"--group_reporting=1"
            )
            rc = subprocess.call(fio_cmd, shell=True)
            if rc != 0:
                print(f"[debug] fio exited rc={rc} (continuing to artifact check)")
        else:
            time.sleep(duration)

    # ----- artifact checks
    print("\n[debug] checking artifacts...")
    failures = []
    sid = os.path.basename(session_dir)

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
            failures.append(f"system_metrics_{sid}.csv has {rows} data rows (expected >=1)")
        print(f"  [OK] system_metrics: {rows} rows")

    md_path = glob.glob(os.path.join(session_dir, "report_*.md"))
    if not md_path:
        failures.append("no report_*.md generated")
    else:
        print(f"  [OK] report: {os.path.basename(md_path[0])}")

    if args.with_fio:
        fio_json = os.path.join(session_dir, "fio_debug.json")
        if not os.path.isfile(fio_json):
            failures.append("missing fio_debug.json")
        else:
            print(f"  [OK] fio output: fio_debug.json")

    if args.with_ebpf:
        # io_profiler writes <device>_<sid>.csv per traced device (e.g. nvme0n1_<sid>.csv)
        io_csvs = [
            p for p in glob.glob(os.path.join(session_dir, f"*_{sid}.csv"))
            if not os.path.basename(p).startswith("system_metrics_")
        ]
        if not io_csvs:
            failures.append("no eBPF device CSV produced (tracer may have failed)")
        else:
            devs = [os.path.basename(p).split("_")[0] for p in io_csvs]
            print(f"  [OK] eBPF csv: {len(io_csvs)} device(s) — {', '.join(devs)}")

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
                    help='fio command line (quoted). Mutually exclusive with --script/--duration')
    pm.add_argument("--script", default=None,
                    help="path to a shell script that issues the workload")
    pm.add_argument("--label", default=None,
                    help="label appended to the session directory name")
    pm.add_argument("--report", default="all",
                    help="report formats to render: html,md,json,png,pdf comma-list or 'all'/'none' (default all)")
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

    pdbg = sub.add_parser("debug", help="developer self-test for code-change verification")
    pdbg.add_argument("--duration", type=int, default=2,
                      help="seconds the smoke window observes (default 2)")
    pdbg.add_argument("--with-ebpf", action="store_true",
                      help="include eBPF I/O tracer (needs sudo + io_trace built)")
    pdbg.add_argument("--with-fio", action="store_true",
                      help="run a small fio against .smoke/smoke.img (needs sudo)")
    pdbg.add_argument("--full", action="store_true",
                      help="enable both --with-ebpf and --with-fio")
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
