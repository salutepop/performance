"""
ASCII/유니코드 차트 프리미티브 — matplotlib 없이 터미널·마크다운에서 보이는
텍스트 그래프. SSH/CUI 환경에서 리포트를 완결적으로(이미지 없이) 읽기 위한
렌더링 레이어다.

모든 함수는 순수 함수다 (stdlib만, 외부 의존성 0). 반환 문자열은 markdown의
``` fence 안에 넣는 것을 전제로 monospace 정렬을 유지한다 — fence 밖에서는
공백이 접혀 정렬이 깨진다.
"""

import math

# 부분 블록 7단계 (1/8 .. 7/8 칸) — 막대의 sub-cell 해상도.
_EIGHTHS = "▏▎▍▌▋▊▉"
# 스파크라인 8단계 (낮음→높음).
_SPARK = "▁▂▃▄▅▆▇█"
# 히트맵 음영 5단계 (0=공백 포함, 낮음→높음).
_SHADE = " ░▒▓█"
# stacked() 채움 fallback 문자 (라벨 첫 글자가 겹칠 때).
_STACK_FALLBACK = "▓▒░█▚▤"


def bar(value, vmax, width=40):
    """value/vmax 비율을 width 칸 막대 문자열로. 1/8 칸 해상도. 항상 width 길이."""
    if not vmax or vmax <= 0 or value is None or value <= 0:
        return " " * width
    frac = min(1.0, value / vmax)
    eighths = int(round(frac * width * 8))
    full, rem = divmod(eighths, 8)
    s = "█" * full + (_EIGHTHS[rem - 1] if rem else "")
    return s.ljust(width)


def hbar(items, width=32, value_fmt="{:.1f}", unit="", vmax=None):
    """라벨 붙은 가로 막대 차트. items: [(label, value)] (value None 허용).

    vmax 미지정 시 최대값으로 정규화. 라벨 폭은 자동 정렬. 멀티라인 문자열."""
    items = list(items)
    if not items:
        return "(no data)"
    lw = max(len(str(l)) for l, _ in items)
    nums = [(v if isinstance(v, (int, float)) else 0) for _, v in items]
    if vmax is None:
        vmax = max(nums) if nums else 0
    out = []
    for label, v in items:
        vnum = v if isinstance(v, (int, float)) else 0
        vs = (value_fmt.format(vnum) + unit) if isinstance(v, (int, float)) else "-"
        out.append(f"{str(label):<{lw}} │{bar(vnum, vmax, width)}│ {vs}")
    return "\n".join(out)


def sparkline(values):
    """값 시퀀스 → 스파크라인 한 줄. None/비숫자는 공백(결손 구간)."""
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = hi - lo
    out = []
    for v in values:
        if not isinstance(v, (int, float)):
            out.append(" ")
        elif span == 0:
            out.append(_SPARK[0])
        else:
            out.append(_SPARK[int(round((v - lo) / span * 7))])
    return "".join(out)


def stacked(segments, width=56, chars=None):
    """비율 누적 가로 막대 + 범례. segments: [(label, value)].

    각 세그먼트는 서로 다른 채움 문자 — 기본은 라벨 첫 글자(대문자), 겹치면
    음영 문자로 fallback. 반환: (bar_str, legend) — legend는 [(char, label,
    value, pct)]. 호출부가 범례를 원하는 포맷으로 렌더한다."""
    segs = [(str(l), (v if isinstance(v, (int, float)) and v > 0 else 0.0))
            for l, v in segments]
    total = sum(v for _, v in segs)
    if total <= 0:
        return " " * width, []

    if chars is None:
        chars, used = [], set()
        for label, _ in segs:
            c = label[0].upper() if label.strip() else "?"
            if c in used:
                c = _STACK_FALLBACK[len(used) % len(_STACK_FALLBACK)]
            used.add(c)
            chars.append(c)

    # 칸 배분 — 누적 반올림으로 각 세그먼트 칸 수를 정하고, 합이 width와
    # 정확히 맞도록 마지막 세그먼트에서 보정.
    cells, done, acc = [], 0, 0.0
    for _, v in segs:
        acc += v
        target = int(round(acc / total * width))
        cells.append(max(0, target - done))
        done += cells[-1]
    cells[-1] += width - sum(cells)

    bar_str = "".join(ch * n for ch, n in zip(chars, cells))
    legend = [(chars[i], segs[i][0], segs[i][1], segs[i][1] / total * 100)
              for i in range(len(segs))]
    return bar_str, legend


def histogram(items, width=32, count_fmt="{:,}", trim_zeros=True):
    """정수 카운트 히스토그램 (가로 막대). items: [(label, count)].

    trim_zeros: 앞뒤(leading/trailing) 0 버킷 제거 — 가운데 0은 유지."""
    items = list(items)
    if trim_zeros:
        lo, hi = 0, len(items)
        while lo < hi and not (items[lo][1] or 0):
            lo += 1
        while hi > lo and not (items[hi - 1][1] or 0):
            hi -= 1
        items = items[lo:hi]
    if not items:
        return "(empty)"
    return hbar(items, width=width, value_fmt=count_fmt)


def heatmap_row(values, log_scale=True):
    """값 배열 → 한 줄 음영 히트맵 (버킷당 1문자). log_scale: 편향 분포용(기본).

    LBA 접근 분포처럼 일부 버킷에 집중되는 데이터는 log 스케일이 적합하다."""
    vals = [(v if isinstance(v, (int, float)) and v > 0 else 0) for v in values]
    vmax = max(vals) if vals else 0
    if vmax <= 0:
        return _SHADE[0] * len(values)
    levels = len(_SHADE) - 1  # 1..levels
    denom = math.log1p(vmax) if log_scale else vmax
    out = []
    for v in vals:
        if v <= 0:
            out.append(_SHADE[0])
            continue
        ratio = (math.log1p(v) / denom) if log_scale else (v / denom)
        out.append(_SHADE[min(levels, 1 + int(ratio * (levels - 1) + 0.5))])
    return "".join(out)


def axis_hint(lo, hi, width, fmt="{:.0f}", unit=""):
    """스파크라인/막대 아래 깔 눈금 힌트. 왼쪽=lo, 오른쪽=hi 정렬된 한 줄."""
    left = fmt.format(lo) + unit
    right = fmt.format(hi) + unit
    pad = max(1, width - len(left) - len(right))
    return left + " " * pad + right
