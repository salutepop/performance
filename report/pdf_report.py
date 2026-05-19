"""
PDF 리포트 — 오프라인 CLI 환경용. matplotlib만 사용 (외부 binary 0).

CLI:
  python3 -m report.pdf_report --session-dir DIR [--session-id SID] [-o out.pdf]

산출: <session-dir>/report_<sid>.pdf
- 표지/요약: report_<sid>.md 본문(없으면 생성 시도)
- 이후 페이지: figs_<sid>/*.png 한 페이지씩 (correlation → multi_* → sys_* → device 순)

figs_<sid>/가 비어 있으면 자동으로 png_report.main() 호출해서 PNG들을 먼저 만든다.
한글 폰트는 Noto Sans CJK KR / NanumGothic / Malgun Gothic 등 시스템 가용한 것을 자동 선택.
"""

import argparse
import glob
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages


_KR_FONT_CANDIDATES = (
    "Noto Sans CJK KR", "Noto Sans CJK JP", "Noto Sans Mono CJK KR",
    "NanumGothic", "NanumBarunGothic",
    "Malgun Gothic", "Apple SD Gothic Neo",
)


def _setup_kr_font():
    """matplotlib에 한글 폰트 등록. 첫 가용 후보 반환, 못 찾으면 None."""
    avail = {f.name for f in fm.fontManager.ttflist}
    for c in _KR_FONT_CANDIDATES:
        if c in avail:
            plt.rcParams["font.family"] = [c, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return c
    return None


def _resolve_sid(session_dir, session_id):
    if session_id:
        return session_id
    topos = sorted(glob.glob(os.path.join(session_dir, "topology_*.json")))
    if not topos:
        return None
    base = os.path.basename(topos[-1])
    return base[len("topology_"):-len(".json")]


def _add_text_pages(pdf, text, title=None, per_page=66):
    """긴 텍스트를 monospace 페이지로 분할. 첫 페이지에만 title."""
    lines = text.splitlines() or [""]
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[""]]
    for i, chunk in enumerate(chunks):
        fig = plt.figure(figsize=(8.5, 11))
        if title and i == 0:
            fig.text(0.06, 0.96, title, fontsize=14, weight="bold")
            y_start = 0.92
        else:
            y_start = 0.96
        fig.text(0.06, y_start, "\n".join(chunk), fontsize=8,
                 family="monospace", va="top")
        # 페이지 번호 (footer)
        fig.text(0.5, 0.02, f"page {i + 1}", ha="center", fontsize=7, color="#888")
        pdf.savefig(fig)
        plt.close(fig)


def _add_image_page(pdf, png_path):
    img = mpimg.imread(png_path)
    # 가로 figure가 차트에 더 잘 맞음 (A4 landscape)
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.imshow(img)
    ax.axis("off")
    fig.text(0.5, 0.02, os.path.basename(png_path), ha="center", fontsize=7, color="#666")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _order_pngs(pngs):
    def key(p):
        n = os.path.basename(p)
        if n.startswith("correlation"): return (0, n)
        if n.startswith("multi_"): return (1, n)
        if n.startswith("sys_"): return (2, n)
        return (3, n)
    return sorted(pngs, key=key)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="세션 산출물 → PDF 리포트 (matplotlib 단독, 오프라인 CLI)"
    )
    p.add_argument("--session-dir", default="ebpf/csv_results")
    p.add_argument("--session-id", default=None)
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    sd = args.session_dir
    sid = _resolve_sid(sd, args.session_id)
    if not sid:
        print(f"[pdf_report] 세션 ID를 찾을 수 없음 (topology_*.json 없음): {sd}", file=sys.stderr)
        return 1

    out_path = args.output or os.path.join(sd, f"report_{sid}.pdf")
    figs_dir = os.path.join(sd, f"figs_{sid}")
    pngs = glob.glob(os.path.join(figs_dir, "*.png"))

    # PNG가 없으면 png_report로 먼저 생성
    if not pngs:
        try:
            from .png_report import main as png_main
            png_main(["--session-dir", sd, "--session-id", sid])
            pngs = glob.glob(os.path.join(figs_dir, "*.png"))
        except Exception as e:
            print(f"[pdf_report] PNG 자동 생성 실패: {e}", file=sys.stderr)

    pngs = _order_pngs(pngs)
    font_used = _setup_kr_font()

    md_path = os.path.join(sd, f"report_{sid}.md")
    md_text = None
    if os.path.exists(md_path):
        with open(md_path, encoding="utf-8") as f:
            md_text = f.read()

    cover_title = f"Performance Report — {sid}"
    pages = 0
    with PdfPages(out_path) as pdf:
        if md_text:
            _add_text_pages(pdf, md_text, title=cover_title)
            pages += 1
        else:
            fig = plt.figure(figsize=(8.5, 11))
            fig.text(0.5, 0.5, cover_title, ha="center", fontsize=18)
            pdf.savefig(fig); plt.close(fig)
            pages += 1

        for png in pngs:
            _add_image_page(pdf, png)
            pages += 1

    font_note = f"font={font_used}" if font_used else "font=default (한글 깨질 수 있음)"
    print(f"[pdf_report] wrote {out_path} ({os.path.getsize(out_path)} bytes, pngs={len(pngs)}, {font_note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
