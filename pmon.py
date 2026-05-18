#!/usr/bin/env python3
"""
pmon — performance monitoring CLI 통합 진입점.

Subcommands:
  run      : fio 워크로드 + io_profiler eBPF 측정 + 자동 리포트 생성
  report   : 기존 세션 산출물에서 HTML/MD/JSON 리포트 생성
  diff     : 두 세션 비교 diff 리포트
  summary  : 단일 세션 JSON 평탄화 export

Usage examples:
  ./pmon.py run --fio "fio --name=t --filename=/tmp/x --rw=randread --bs=4k --iodepth=8 --size=64M --runtime=3 --time_based --direct=1 --ioengine=libaio"
  ./pmon.py run --script ebpf/fio.sh -m libaio -i 1
  ./pmon.py report                                            # 가장 최근 세션
  ./pmon.py report --session-id 20260519_002145
  ./pmon.py diff --baseline 20260519_001707 --candidate 20260519_002145
  ./pmon.py summary
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime


ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SESSION_DIR = os.path.join(ROOT, "ebpf", "csv_results")


def cmd_run(args):
    """io_profiler.py 호출 → 종료 후 report 자동 생성."""
    if not args.fio and not args.script:
        print("[pmon] --fio 또는 --script 중 하나는 필수", file=sys.stderr)
        return 2

    ebpf_dir = os.path.join(ROOT, "ebpf")
    cmd = ["python3", "io_profiler.py", "-m", args.mode, "-i", str(args.interval)]
    if args.fio:
        cmd += ["-c", args.fio]
    else:
        cmd += ["-f", args.script]

    print(f"[pmon] run start (mode={args.mode}, interval={args.interval}s)")
    rc = subprocess.call(cmd, cwd=ebpf_dir)
    if rc != 0:
        print(f"[pmon] io_profiler exit={rc}", file=sys.stderr)
        return rc

    # 자동 리포트 — 가장 최근 세션이 방금 만든 것.
    if args.report != "none":
        time.sleep(0.5)  # 파일시스템 stat 안정화
        _generate_reports(DEFAULT_SESSION_DIR, None, args.report)
    return 0


def cmd_report(args):
    return 0 if _generate_reports(args.session_dir, args.session_id, args.format) else 2


def cmd_diff(args):
    from report.diff import main as diff_main
    return diff_main(["--baseline", args.baseline,
                      "--candidate", args.candidate,
                      "--session-dir", args.session_dir]
                     + (["-o", args.output] if args.output else []))


def cmd_summary(args):
    from report.summary import main as summary_main
    argv = ["--session-dir", args.session_dir]
    if args.session_id:
        argv += ["--session-id", args.session_id]
    if args.output:
        argv += ["-o", args.output]
    return summary_main(argv)


def _generate_reports(session_dir, session_id, fmt):
    """fmt는 콤마 구분 (html, md, json) 또는 'all', 'none'."""
    if fmt == "none":
        return True
    targets = ["html", "md", "json"] if fmt == "all" else [f.strip() for f in fmt.split(",")]
    base_argv = ["--session-dir", session_dir]
    if session_id:
        base_argv += ["--session-id", session_id]

    ok = True
    if "html" in targets:
        from report.html_report import main as html_main
        ok &= html_main(base_argv) == 0
    if "md" in targets:
        from report.md_report import main as md_main
        ok &= md_main(base_argv) == 0
    if "json" in targets:
        from report.summary import main as summary_main
        ok &= summary_main(base_argv) == 0
    return ok


def main(argv=None):
    p = argparse.ArgumentParser(prog="pmon", description="Performance Monitoring CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="fio + eBPF + 자동 리포트")
    pr.add_argument("--fio", help="fio 명령행 (따옴표로 감싸기). --script와 배타적")
    pr.add_argument("--script", help="fio 워크로드 스크립트 파일 경로")
    pr.add_argument("-m", "--mode", default="libaio", choices=["generic", "libaio", "iouring"])
    pr.add_argument("-i", "--interval", type=float, default=1.0)
    pr.add_argument("--report", default="all",
                    help="자동 생성할 리포트 포맷: html,md,json 콤마구분 or 'all' or 'none' (기본: all)")
    pr.set_defaults(func=cmd_run)

    prep = sub.add_parser("report", help="기존 세션 산출물에서 리포트 생성")
    prep.add_argument("--session-dir", default=DEFAULT_SESSION_DIR)
    prep.add_argument("--session-id", default=None)
    prep.add_argument("--format", default="all",
                      help="html,md,json 콤마구분 or 'all' (기본: all)")
    prep.set_defaults(func=cmd_report)

    pdi = sub.add_parser("diff", help="두 세션 비교 diff 리포트")
    pdi.add_argument("--baseline", required=True)
    pdi.add_argument("--candidate", required=True)
    pdi.add_argument("--session-dir", default=DEFAULT_SESSION_DIR)
    pdi.add_argument("-o", "--output", default=None)
    pdi.set_defaults(func=cmd_diff)

    psu = sub.add_parser("summary", help="단일 세션 JSON 평탄화")
    psu.add_argument("--session-dir", default=DEFAULT_SESSION_DIR)
    psu.add_argument("--session-id", default=None)
    psu.add_argument("-o", "--output", default=None)
    psu.set_defaults(func=cmd_summary)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
