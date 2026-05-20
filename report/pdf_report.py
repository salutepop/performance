"""
PDF 리포트 — 오프라인 CLI 환경용. matplotlib만 사용 (외부 binary 0).

CLI:
  python3 -m report.pdf_report --session-dir DIR [--session-id SID] [-o out.pdf]

산출: <session-dir>/report_<sid>.pdf
- 표지/요약: report_<sid>.md 본문을 구조적으로 렌더 (heading / bullet / table)
  · `|...|` 연속 라인은 matplotlib `ax.table()`로 진짜 표로 그림
  · 헤딩(`#`/`##`/`###`)은 가중치, bullet(`- `)은 들여쓰기
- 이후 페이지: figs_<sid>/*.png 한 페이지씩 (correlation → multi_* → sys_* → device 순)

figs_<sid>/가 비어 있으면 자동으로 png_report.main() 호출해서 PNG들을 먼저 만든다.
한글 폰트는 Noto Sans CJK KR / NanumGothic / Malgun Gothic 등 시스템 가용한 것을 자동 선택.
"""

import argparse
import glob
import os
import re
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


_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _parse_md_blocks(text):
    """Markdown 라인을 블록 시퀀스로 변환.

    Returns list of dicts:
      {'kind': 'heading', 'level': int, 'text': str}
      {'kind': 'bullet',  'text': str}
      {'kind': 'para',    'text': str}
      {'kind': 'blank'}
      {'kind': 'table',   'header': [str], 'rows': [[str]], 'align': [str]}
    """
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 표: 연속된 `|...|` 라인. 두 번째 라인이 separator면 첫 줄을 header로 사용.
        if _TABLE_LINE.match(line):
            tbl_lines = []
            while i < len(lines) and _TABLE_LINE.match(lines[i]):
                tbl_lines.append(lines[i])
                i += 1
            cells = [_split_md_row(r) for r in tbl_lines]
            header, rows, align = None, cells, None
            if len(cells) >= 2 and _TABLE_SEP.match(tbl_lines[1]):
                header = cells[0]
                align = _parse_align_row(cells[1])
                rows = cells[2:]
            blocks.append({"kind": "table", "header": header, "rows": rows, "align": align})
            continue

        if not stripped:
            blocks.append({"kind": "blank"})
        elif stripped.startswith("###"):
            blocks.append({"kind": "heading", "level": 3, "text": stripped.lstrip("# ").strip()})
        elif stripped.startswith("##"):
            blocks.append({"kind": "heading", "level": 2, "text": stripped.lstrip("# ").strip()})
        elif stripped.startswith("#"):
            blocks.append({"kind": "heading", "level": 1, "text": stripped.lstrip("# ").strip()})
        elif stripped.startswith("- "):
            blocks.append({"kind": "bullet", "text": stripped[2:].strip()})
        else:
            blocks.append({"kind": "para", "text": stripped})
        i += 1
    return blocks


def _split_md_row(line):
    """`| a | b | c |` → ['a', 'b', 'c']"""
    s = line.strip()
    if s.startswith("|"): s = s[1:]
    if s.endswith("|"): s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _parse_align_row(cells):
    out = []
    for c in cells:
        c = c.strip()
        if c.startswith(":") and c.endswith(":"): out.append("center")
        elif c.endswith(":"):                     out.append("right")
        elif c.startswith(":"):                   out.append("left")
        else:                                     out.append("right")
    return out


def _strip_md_emphasis(s):
    """`**bold**` / `*em*` / `_em_` / `` `code` `` 표식 제거 (PDF 본문은 plain text 렌더)."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", s)
    s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s


def _render_blocks_to_pdf(pdf, blocks, title=None, page_size=(8.5, 11)):
    """블록 시퀀스를 PDF 페이지로 렌더. 페이지가 가득 차면 새 페이지로 넘김."""
    page_w, page_h = page_size
    margin_x, margin_top, margin_bot = 0.06, 0.04, 0.04  # figure 비율 좌표

    state = {"page": 0, "fig": None, "y": 0.0, "has_title": bool(title)}

    def _new_page():
        state["page"] += 1
        fig = plt.figure(figsize=page_size)
        state["fig"] = fig
        state["y"] = 1 - margin_top
        if state["has_title"] and state["page"] == 1:
            fig.text(margin_x, state["y"], title, fontsize=15, weight="bold")
            state["y"] -= 0.035
            # 얇은 구분선
            fig.add_artist(plt.Line2D([margin_x, 1 - margin_x],
                                      [state["y"], state["y"]],
                                      color="#888", linewidth=0.4))
            state["y"] -= 0.02

    def _finalize():
        if state["fig"] is None:
            return
        fig = state["fig"]
        fig.text(0.5, 0.02, f"page {state['page']}", ha="center", fontsize=7, color="#888")
        pdf.savefig(fig)
        plt.close(fig)
        state["fig"] = None

    def _ensure(min_h):
        if state["fig"] is None or state["y"] - min_h < margin_bot:
            _finalize()
            _new_page()

    _new_page()
    for blk in blocks:
        k = blk["kind"]
        if k == "blank":
            state["y"] -= 0.012
        elif k == "heading":
            lvl = blk["level"]
            size, weight, gap_above, gap_below = {
                1: (14, "bold",   0.012, 0.014),
                2: (12, "bold",   0.018, 0.010),
                3: (10, "bold",   0.012, 0.008),
            }[lvl]
            state["y"] -= gap_above
            _ensure(0.035)
            state["fig"].text(margin_x, state["y"], _strip_md_emphasis(blk["text"]),
                              fontsize=size, weight=weight, color="#222")
            state["y"] -= gap_below
        elif k == "bullet":
            _ensure(0.02)
            state["fig"].text(margin_x + 0.005, state["y"], "·", fontsize=10, color="#444")
            state["fig"].text(margin_x + 0.022, state["y"],
                              _strip_md_emphasis(blk["text"]), fontsize=9)
            state["y"] -= 0.018
        elif k == "para":
            _ensure(0.02)
            state["fig"].text(margin_x, state["y"], _strip_md_emphasis(blk["text"]),
                              fontsize=9, color="#333")
            state["y"] -= 0.018
        elif k == "table":
            header = blk["header"]
            rows = blk["rows"]
            align = blk["align"]
            n_rows = len(rows) + (1 if header else 0)
            row_h = 0.024  # figure 단위
            table_h = row_h * n_rows + 0.012  # 약간의 패딩
            _ensure(table_h + 0.01)
            _draw_table(state["fig"], header, rows, align,
                        x=margin_x, y_top=state["y"],
                        width=1 - 2 * margin_x, row_h=row_h)
            state["y"] -= table_h + 0.012

    _finalize()


def _draw_table(fig, header, rows, align, x, y_top, width, row_h):
    """matplotlib `ax.table()` 기반 표 렌더.

    Figure 좌표계에 작은 ax를 만들어 표만 그린다. 컬럼 너비는 헤더+셀 최대 길이로 가중.
    """
    n_rows = len(rows) + (1 if header else 0)
    height = row_h * n_rows
    # 좌상단 (x, y_top) - 하단 (x + width, y_top - height) 사이에 표 배치
    ax = fig.add_axes([x, y_top - height, width, height])
    ax.set_axis_off()

    # 빈 표 처리
    if not rows and not header:
        return

    if not header:
        # header가 없는 표는 첫 행을 header로 가정해도 되지만, 안전하게 그냥 데이터로 그림
        header = [""] * (len(rows[0]) if rows else 1)

    n_cols = len(header)
    # 컬럼별 최대 길이로 weight 계산
    col_lens = [max(len(str(header[c])), *(len(str(r[c]) if c < len(r) else "") for r in rows)) for c in range(n_cols)] if rows else [len(str(h)) for h in header]
    total = sum(col_lens) or n_cols
    col_widths = [max(0.06, cl / total) for cl in col_lens]
    # normalize so they sum to 1
    s = sum(col_widths)
    col_widths = [w / s for w in col_widths]

    # align 정규화
    align_loc = []
    for i in range(n_cols):
        a = (align or ["right"] * n_cols)[i] if i < len(align or []) else "right"
        align_loc.append({"left": "left", "right": "right", "center": "center"}.get(a, "right"))

    cell_text = [[_strip_md_emphasis(str(c)) for c in (header)]]
    for r in rows:
        cell_text.append([_strip_md_emphasis(str(r[c]) if c < len(r) else "") for c in range(n_cols)])

    table = ax.table(
        cellText=cell_text[1:] if rows else [[""] * n_cols],
        colLabels=cell_text[0],
        colWidths=col_widths,
        cellLoc="right",
        colLoc="center",
        loc="upper left",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    # 행 높이를 row_h에 맞춤 (ax 높이 기준 normalized — ax 자체가 figure 좌표라
    # 1.0이면 ax 전체 높이가 한 행임. 우리는 n_rows 행 → 각 셀 1/n_rows)
    for (r, c), cell in table.get_celld().items():
        cell.set_height(1.0 / max(1, n_rows))
        cell.set_edgecolor("#cccccc")
        cell.set_linewidth(0.4)
        if r == 0:
            cell.set_facecolor("#f0f2f5")
            cell.set_text_props(weight="bold", ha="center", color="#222")
        else:
            cell.set_text_props(ha=align_loc[c] if c < len(align_loc) else "right")
            if r % 2 == 0:
                cell.set_facecolor("#fafbfc")


def _add_cover(pdf, md_text, title):
    """Markdown 본문을 표 렌더링 포함해서 PDF 페이지로 변환.

    첫 블록이 H1이면 PDF 표제와 중복되므로 스킵.
    """
    blocks = _parse_md_blocks(md_text)
    # 선두의 blank/H1 dedup: H1 한 개까지만 제거 (실제 본문 H1은 보존)
    j = 0
    while j < len(blocks) and blocks[j]["kind"] == "blank":
        j += 1
    if j < len(blocks) and blocks[j]["kind"] == "heading" and blocks[j]["level"] == 1:
        blocks = blocks[:j] + blocks[j + 1:]
    _render_blocks_to_pdf(pdf, blocks, title=title)


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
        description="Session artifacts -> PDF report (matplotlib only, offline CLI)"
    )
    p.add_argument("--session-dir", default="results")
    p.add_argument("--session-id", default=None)
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    sd = args.session_dir
    sid = _resolve_sid(sd, args.session_id)
    if not sid:
        print(f"[pdf_report] no session id found (no topology_*.json in {sd})", file=sys.stderr)
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
            print(f"[pdf_report] auto-generating PNGs failed: {e}", file=sys.stderr)

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
            _add_cover(pdf, md_text, title=cover_title)
            pages += 1
        else:
            fig = plt.figure(figsize=(8.5, 11))
            fig.text(0.5, 0.5, cover_title, ha="center", fontsize=18)
            pdf.savefig(fig); plt.close(fig)
            pages += 1

        for png in pngs:
            _add_image_page(pdf, png)
            pages += 1

    font_note = f"font={font_used}" if font_used else "font=default"
    print(f"[pdf_report] wrote {out_path} ({os.path.getsize(out_path)} bytes, pngs={len(pngs)}, {font_note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
