"""
`python3 -m report ...` 진입점.

서브커맨드 없이 `--format` 콤마구분으로 md/json/png/pdf 일괄 생성하는 편의 진입점.
세부 subcommand가 필요하면 프로젝트 루트의 pmon.py 또는
`python3 -m report.{md_report,summary,png_report,pdf_report,diff}` 직접 호출.

CLI:
  python3 -m report [--session-dir DIR] [--session-id SID] [--format all|md,json,png,pdf]
"""

import argparse
import sys


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python3 -m report",
        description="Session artifacts -> md/json/png/pdf bundled report (convenience entry). Use -m report.diff for session diff."
    )
    p.add_argument("--session-dir", default="results")
    p.add_argument("--session-id", default=None)
    p.add_argument("--format", default="all",
                   help="Comma-separated subset of md,json,png,pdf or 'all' (default: all). png/pdf auto-skip when matplotlib is missing.")
    args = p.parse_args(argv)

    targets = ["md", "json", "png", "pdf"] if args.format == "all" else [t.strip() for t in args.format.split(",")]
    base_argv = ["--session-dir", args.session_dir]
    if args.session_id:
        base_argv += ["--session-id", args.session_id]

    rc = 0
    if "md" in targets:
        from .md_report import main as md_main
        rc |= md_main(base_argv) or 0
    if "json" in targets:
        from .summary import main as summary_main
        rc |= summary_main(base_argv) or 0
    if "png" in targets:
        try:
            from .png_report import main as png_main
        except ImportError as e:
            print(f"[!] PNG report skipped (matplotlib missing or import failed): {e}")
        else:
            rc |= png_main(base_argv) or 0
    if "pdf" in targets:
        try:
            from .pdf_report import main as pdf_main
        except ImportError as e:
            print(f"[!] PDF report skipped (matplotlib missing or import failed): {e}")
        else:
            rc |= pdf_main(base_argv) or 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
