"""
`python3 -m report ...` 진입점.

서브커맨드 없이 `--format` 콤마구분으로 html/md/json 일괄 생성하는 편의 진입점.
세부 subcommand가 필요하면 프로젝트 루트의 pmon.py 또는
`python3 -m report.{html_report,md_report,summary,diff}` 직접 호출.

CLI:
  python3 -m report [--session-dir DIR] [--session-id SID] [--format all|html,md,json]
"""

import argparse
import sys


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python3 -m report",
        description="세션 산출물 → html/md/json 일괄 리포트 (편의 진입점). diff는 -m report.diff로 직접 호출."
    )
    p.add_argument("--session-dir", default="ebpf/csv_results")
    p.add_argument("--session-id", default=None)
    p.add_argument("--format", default="all",
                   help="html,md,json,png 콤마 구분 또는 'all' (기본: all). 'all'은 html+md+json (png 제외, deps 무거움).")
    args = p.parse_args(argv)

    targets = ["html", "md", "json"] if args.format == "all" else [t.strip() for t in args.format.split(",")]
    base_argv = ["--session-dir", args.session_dir]
    if args.session_id:
        base_argv += ["--session-id", args.session_id]

    rc = 0
    if "html" in targets:
        from .html_report import main as html_main
        rc |= html_main(base_argv) or 0
    if "md" in targets:
        from .md_report import main as md_main
        rc |= md_main(base_argv) or 0
    if "json" in targets:
        from .summary import main as summary_main
        rc |= summary_main(base_argv) or 0
    if "png" in targets:
        from .png_report import main as png_main
        rc |= png_main(base_argv) or 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
