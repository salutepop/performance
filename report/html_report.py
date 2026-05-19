"""
세션 산출물(topology_*.json, system_metrics_*.csv, <device>_*.csv)을
자기완결 HTML로 변환. 외부 리소스 0 (인터넷 없는 환경에서도 동작).

baseline 버전: 표 dump + topology 요약. 차트는 후속 task.

CLI:
  python3 -m report.html_report --session-dir csv_results/ [--session-id SID] [-o out.html]
  python3 -m report.html_report  # cwd 기준 ebpf/csv_results 자동 탐색
"""

import argparse
import csv
import glob
import html
import json
import os
import re
import sys
from datetime import datetime


def _discover_session(session_dir):
    """가장 최근 topology_*.json 의 session_id 반환. 없으면 None."""
    paths = sorted(glob.glob(os.path.join(session_dir, "topology_*.json")), reverse=True)
    if not paths:
        return None
    m = re.search(r"topology_(\d{8}_\d{6})\.json$", paths[0])
    return m.group(1) if m else None


def _load_topology(session_dir, sid):
    p = os.path.join(session_dir, f"topology_{sid}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def _load_csv(path):
    """(header_list, rows_list) 반환. 없으면 (None, None)."""
    if not os.path.exists(path):
        return None, None
    with open(path, newline="") as f:
        r = csv.reader(f)
        try:
            header = next(r)
        except StopIteration:
            return [], []
        rows = list(r)
    return header, rows


def _render_table(header, rows, max_rows=200):
    """간단한 HTML 테이블. 너무 길면 앞 max_rows만 렌더링."""
    if not header:
        return "<p><em>(empty)</em></p>"
    if not rows:
        return f"<table><thead><tr>{''.join(f'<th>{html.escape(c)}</th>' for c in header)}</tr></thead><tbody><tr><td colspan='{len(header)}'><em>(no data rows)</em></td></tr></tbody></table>"
    truncated = len(rows) > max_rows
    rows_to_render = rows[:max_rows]
    out = ["<table><thead><tr>"]
    for c in header:
        out.append(f"<th>{html.escape(c)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows_to_render:
        out.append("<tr>")
        for cell in row:
            out.append(f"<td>{html.escape(str(cell))}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    if truncated:
        out.append(f"<p class='note'>… {len(rows) - max_rows} more rows omitted (총 {len(rows)}행)</p>")
    return "".join(out)


_OP_COLORS = {
    "read": "#0a84ff", "write": "#ff453a",
    "read_ahead": "#30d158", "flush": "#bf5af2", "discard": "#8e8e93",
}

# 다중 시리즈 자동 색 팔레트 (NUMA node, NVMe controller, GPU 등).
_PALETTE = ["#0a84ff", "#ff453a", "#30d158", "#ff9f0a", "#bf5af2", "#5e5ce6", "#64d2ff", "#ffd60a"]


def _build_system_series(header, rows):
    """system_metrics CSV → labels[] + 4종 chart 데이터.
    {labels, cpu:{label:[]}, irq:{label:[]}, mem:{label:[]}, gpu:{label:[]}}."""
    if not header or not rows:
        return None
    ts_i = -1
    try:
        ts_i = header.index("timestamp")
    except ValueError:
        return None

    labels = [row[ts_i] for row in rows]

    def _series_for(predicate):
        out = {}
        for i, name in enumerate(header):
            if predicate(name):
                col = []
                for row in rows:
                    if i >= len(row) or row[i] in ("", None):
                        col.append(None)
                    else:
                        try:
                            col.append(float(row[i]))
                        except ValueError:
                            col.append(None)
                out[name] = col
        return out

    cpu_series = _series_for(lambda n: n.startswith("node") and (
        n.endswith("_user_pct") or n.endswith("_sys_pct") or n.endswith("_iowait_pct")))
    irq_series = _series_for(lambda n: n.endswith("_irq_per_s") and n.startswith("nvme"))
    mem_series = _series_for(lambda n: n in ("mem_dirty_mb", "mem_writeback_mb"))
    # GPU SM 단독은 % / power는 W → 단위 다르므로 두 차트 분리. 우선 SM과 power 한 패널에 dual y-axis보단 simple하게 같이 출력.
    gpu_series = _series_for(lambda n: n.startswith("gpu") and (
        n.endswith("_sm_pct") or n.endswith("_pwr_w") or n.endswith("_mem_pct")))

    return {"labels": labels, "cpu": cpu_series, "irq": irq_series, "mem": mem_series, "gpu": gpu_series}


def _render_system_charts(payload):
    """system_metrics 4종 line chart 렌더링. payload는 _build_system_series 출력."""
    if not payload:
        return ""
    has_gpu = bool(payload.get("gpu"))
    has_irq = bool(payload.get("irq"))
    has_mem = bool(payload.get("mem"))
    has_cpu = bool(payload.get("cpu"))
    if not (has_cpu or has_irq or has_mem or has_gpu):
        return ""

    canvases = []
    if has_cpu: canvases.append(("cpu", "CPU % (per NUMA node)", "%"))
    if has_irq: canvases.append(("irq", "NVMe IRQ rate", "IRQ/s"))
    if has_mem: canvases.append(("mem", "Memory dirty/writeback", "MB"))
    if has_gpu: canvases.append(("gpu", "GPU utilization & power", "% / W"))

    parts = ["<div class='chart-row'>"]
    for key, _, _ in canvases:
        parts.append(f"<div class='chart-cell'><canvas id='chart_sys_{key}'></canvas></div>")
    parts.append("</div>")

    payload_json = json.dumps(payload, separators=(",", ":"))
    specs = json.dumps(canvases, separators=(",", ":"))
    palette = json.dumps(_PALETTE)
    parts.append(f"""<script>(function(){{
if (typeof Chart === 'undefined') {{
  document.querySelectorAll('[id^="chart_sys_"]').forEach(c => c.parentNode.innerHTML = '<p class=\\'chart-warn\\'>Chart.js CDN unreachable.</p>');
  return;
}}
const payload = {payload_json};
const specs = {specs};
const palette = {palette};
specs.forEach(function(spec) {{
  const key = spec[0], title = spec[1], ylabel = spec[2];
  const data = payload[key] || {{}};
  const labels = Object.keys(data);
  const datasets = labels.map(function(label, i) {{
    return {{label: label, data: data[label], borderColor: palette[i % palette.length],
             backgroundColor: 'transparent', pointRadius: 1, tension: 0.2, spanGaps: true}};
  }});
  const cid = 'chart_sys_' + key;
  const ctx = document.getElementById(cid);
  if (!ctx) return;
  new Chart(ctx.getContext('2d'), {{
    type: 'line', data: {{labels: payload.labels, datasets: datasets}},
    options: {{responsive: true, maintainAspectRatio: false, animation: false,
               plugins: {{title: {{display: true, text: title}}, legend: {{position: 'bottom'}}}},
               scales: {{y: {{title: {{display: true, text: ylabel}}, beginAtZero: true}},
                         x: {{ticks: {{maxTicksLimit: 12}}}}}}}}
  }});
}});
}})();</script>""")
    return "".join(parts)


def _fmt_num(v, unit="", prec=1):
    if v is None:
        return "-"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.{prec}f}M{unit}"
    if abs(v) >= 1_000:
        return f"{v/1_000:.{prec}f}K{unit}"
    return f"{v:.{prec}f}{unit}" if v != int(v) else f"{int(v)}{unit}"


def _render_summary(dev_aggs, sys_agg):
    """상단 executive summary: 카드 + Top findings."""
    if not dev_aggs and not sys_agg:
        return ""

    # 디바이스 합산값
    total_read = 0.0
    total_write = 0.0
    peak_bw = 0.0
    peak_d2c_us = 0.0
    sqcq_avgs = []
    for da in dev_aggs.values():
        for op, key in (("read", "iops"), ("read_ahead", "iops")):
            v = (da.get(op) or {}).get(key, {}).get("sum")
            if v: total_read += v
        v = (da.get("write") or {}).get("iops", {}).get("sum")
        if v: total_write += v
        for op in da:
            if op.startswith("_"):
                continue
            bw = (da[op].get("bw") or {}).get("max") or 0
            if bw > peak_bw: peak_bw = bw
            d = (da[op].get("d2c") or {}).get("max") or 0
            if d > peak_d2c_us: peak_d2c_us = d
        s = (da.get("_sqcq_diff_ratio") or {}).get("avg")
        if s is not None: sqcq_avgs.append(s)

    # 시스템 peak
    cpu = sys_agg.get("cpu", {})
    iowait_peak = max((s.get("max", 0) or 0) for k, s in cpu.items() if k.endswith("_iowait_pct")) if cpu else 0
    sys_peak = max((s.get("max", 0) or 0) for k, s in cpu.items() if k.endswith("_sys_pct")) if cpu else 0
    gpu = sys_agg.get("gpu", {})
    gpu_pwr_peak = max((s.get("max", 0) or 0) for k, s in gpu.items() if "_pwr_w" in k) if gpu else None

    sqcq_avg = (sum(sqcq_avgs) / len(sqcq_avgs)) if sqcq_avgs else 0

    cards = [
        ("Read IOPS (total)", _fmt_num(total_read), "", ""),
        ("Write IOPS (total)", _fmt_num(total_write), "", ""),
        ("Peak BW", _fmt_num(peak_bw, " MB/s", 1), "", ""),
        ("Worst D2C interval avg", _fmt_num(peak_d2c_us, " us", 1),
         "warn" if peak_d2c_us > 500 else "", "tail spike 지점 잠재력"),
        ("SQ↔CQ diff", f"{sqcq_avg*100:.1f}%",
         "bad" if sqcq_avg > 0.2 else ("warn" if sqcq_avg > 0.05 else ""),
         "cross-CPU completion 비율"),
        ("CPU iowait peak", f"{iowait_peak:.1f}%",
         "bad" if iowait_peak > 10 else ("warn" if iowait_peak > 2 else ""), ""),
        ("CPU sys peak", f"{sys_peak:.1f}%", "warn" if sys_peak > 50 else "", ""),
    ]
    if gpu_pwr_peak is not None:
        cards.append(("GPU power peak", _fmt_num(gpu_pwr_peak, " W", 0),
                      "warn" if gpu_pwr_peak > 50 else "", "GPU 활동 흔적"))

    out = ["<div class='summary-cards'>"]
    for label, val, klass, hint in cards:
        cls = f"card {klass}".strip()
        h_html = f"<div class='h'>{html.escape(hint)}</div>" if hint else ""
        out.append(f"<div class='{cls}'><div class='l'>{html.escape(label)}</div>"
                   f"<div class='v'>{html.escape(val)}</div>{h_html}</div>")
    out.append("</div>")

    # Top findings (md_report 헬퍼 재사용)
    try:
        from .md_report import _top_findings
        findings = _top_findings(sys_agg, dev_aggs)
        if findings:
            out.append("<div class='findings'><strong>Top findings</strong><ul>")
            for f in findings:
                # md 식으로 **bold** 들어가는 경우 처리
                f_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(f))
                # html.escape가 <strong>의 <>도 escape했으니 복구
                f_html = f_html.replace("&lt;strong&gt;", "<strong>").replace("&lt;/strong&gt;", "</strong>")
                out.append(f"<li>{f_html}</li>")
            out.append("</ul></div>")
    except Exception:
        pass
    return "".join(out)


def _render_correlation_chart(sys_header, sys_rows, device_csv_paths):
    """1개 dual-axis 차트로 I/O와 system load 상관 시각화.
       왼쪽 Y = device 총 IOPS (디바이스별 색 분리), 오른쪽 Y = sys%/iowait% (점선)."""
    if not sys_header or not sys_rows or not device_csv_paths:
        return ""
    try:
        ts_i = sys_header.index("timestamp")
    except ValueError:
        return ""
    labels = [r[ts_i] for r in sys_rows if ts_i < len(r)]
    if not labels:
        return ""

    # system: iowait 합 + sys% 합 (모든 NUMA 노드)
    iowait_cols = [c for c in sys_header if c.endswith("_iowait_pct")]
    sys_cols = [c for c in sys_header if c.endswith("_sys_pct")]
    iow_idx = [sys_header.index(c) for c in iowait_cols]
    sys_idx = [sys_header.index(c) for c in sys_cols]

    def _safe_sum(row, idxs):
        s = 0.0
        for i in idxs:
            if i < len(row) and row[i] not in ("", None):
                try:
                    s += float(row[i])
                except ValueError:
                    pass
        return s

    iowait_sum = [_safe_sum(r, iow_idx) for r in sys_rows]
    sys_sum = [_safe_sum(r, sys_idx) for r in sys_rows]

    # device: timestamp별 op 합산 IOPS
    dev_aligned = {}
    for dpath in device_csv_paths:
        h, r = _load_csv(dpath)
        if not h:
            continue
        try:
            dts_i = h.index("timestamp")
            iops_i = h.index("iops_interval")
        except ValueError:
            continue
        dname = re.sub(r"_\d{8}_\d{6}\.csv$", "", os.path.basename(dpath))
        bucket = {}
        for row in r:
            ts = row[dts_i] if dts_i < len(row) else ""
            try:
                v = float(row[iops_i]) if iops_i < len(row) and row[iops_i] not in ("", None) else 0.0
            except ValueError:
                v = 0.0
            bucket[ts] = bucket.get(ts, 0) + v
        dev_aligned[dname] = [bucket.get(ts) for ts in labels]

    payload = {
        "labels": labels,
        "iowait": iowait_sum,
        "sys": sys_sum,
        "devs": dev_aligned,
    }
    palette = json.dumps(_PALETTE)
    payload_json = json.dumps(payload, separators=(",", ":"))

    return f"""<div class='chart-row' style='grid-template-columns: 1fr;'>
<div class='chart-cell' style='height: 360px;'><canvas id='chart_corr'></canvas></div>
</div>
<script>(function() {{
if (typeof Chart === 'undefined') {{
  const e = document.getElementById('chart_corr');
  if (e) e.parentNode.innerHTML = '<p class=\\'chart-warn\\'>Chart.js CDN unreachable.</p>';
  return;
}}
const p = {payload_json};
const palette = {palette};
const devDatasets = Object.keys(p.devs).map(function(dn, i) {{
  return {{label: 'IOPS '+dn, data: p.devs[dn], yAxisID: 'y',
           borderColor: palette[i % palette.length],
           backgroundColor: 'transparent', pointRadius: 1, tension: 0.2, spanGaps: true}};
}});
const sysDatasets = [
  {{label: 'iowait % (sum)', data: p.iowait, yAxisID: 'y1',
    borderColor: '#ef4444', borderDash: [6,3], backgroundColor: 'transparent',
    pointRadius: 1, tension: 0.2}},
  {{label: 'sys % (sum)', data: p.sys, yAxisID: 'y1',
    borderColor: '#f59e0b', borderDash: [2,2], backgroundColor: 'transparent',
    pointRadius: 1, tension: 0.2}},
];
const ctx = document.getElementById('chart_corr');
if (!ctx) return;
new Chart(ctx.getContext('2d'), {{
  type: 'line',
  data: {{labels: p.labels, datasets: devDatasets.concat(sysDatasets)}},
  options: {{responsive: true, maintainAspectRatio: false, animation: false,
    plugins: {{title: {{display: true, text: 'I/O × System correlation (left: IOPS, right: CPU %)'}},
               legend: {{position: 'bottom'}}}},
    scales: {{
      y:  {{type: 'linear', position: 'left',  title: {{display: true, text: 'IOPS'}}, beginAtZero: true}},
      y1: {{type: 'linear', position: 'right', title: {{display: true, text: '% CPU'}}, beginAtZero: true, grid: {{drawOnChartArea: false}}}},
      x:  {{ticks: {{maxTicksLimit: 12}}}}
    }}
  }}
}});
}})();</script>"""


def _build_device_series(header, rows):
    """device CSV → (labels[], series{op:{iops,bw,d2c,p50,p99}}). 모든 op timestamp 통합·정렬."""
    if not header or not rows:
        return [], {}
    try:
        ts_i = header.index("timestamp")
        op_i = header.index("operation")
        iops_i = header.index("iops_interval")
        bw_i = header.index("bandwidth_mb_s_interval")
        d2c_i = header.index("d2c_avg_us_interval")
    except ValueError:
        return [], {}
    p50_i = header.index("d2c_p50_us") if "d2c_p50_us" in header else -1
    p99_i = header.index("d2c_p99_us") if "d2c_p99_us" in header else -1

    def _f(v):
        try:
            return float(v) if v not in ("", None) else None
        except ValueError:
            return None

    labels = []
    seen = set()
    op_data = {}
    for row in rows:
        ts = row[ts_i] if ts_i < len(row) else ""
        op = row[op_i] if op_i < len(row) else "?"
        if ts not in seen:
            labels.append(ts)
            seen.add(ts)
        op_data.setdefault(op, {})[ts] = {
            "iops": _f(row[iops_i]) if iops_i < len(row) else None,
            "bw":   _f(row[bw_i])   if bw_i < len(row) else None,
            "d2c":  _f(row[d2c_i])  if d2c_i < len(row) else None,
            "p50":  _f(row[p50_i])  if 0 <= p50_i < len(row) else None,
            "p99":  _f(row[p99_i])  if 0 <= p99_i < len(row) else None,
        }
    series = {}
    for op, by_ts in op_data.items():
        series[op] = {
            "iops": [by_ts.get(t, {}).get("iops") for t in labels],
            "bw":   [by_ts.get(t, {}).get("bw")   for t in labels],
            "d2c":  [by_ts.get(t, {}).get("d2c")  for t in labels],
            "p50":  [by_ts.get(t, {}).get("p50")  for t in labels],
            "p99":  [by_ts.get(t, {}).get("p99")  for t in labels],
        }
    return labels, series


def _build_lba_heatmap(header, rows):
    """device CSV → {timestamps:[], buckets: [[count per ts] × 64]}.
    각 timestamp에서 모든 operation의 lba_N 합계를 취하고, 이전 timestamp 와의 delta 사용.
    """
    if not header or not rows:
        return None
    try:
        ts_i = header.index("timestamp")
    except ValueError:
        return None
    # CSV 헤더에서 연속된 lba_N 컬럼 동적 카운트 (LBA_BUCKETS 변경에 자동 대응)
    lba_indices = []
    for i in range(1024):  # 안전한 상한
        try:
            lba_indices.append(header.index(f"lba_{i}"))
        except ValueError:
            break
    if not lba_indices:
        return None
    nb = len(lba_indices)

    # timestamp 단위로 op별 합계 → row 합 (op 무관, 디스크 전체 접근 분포)
    ts_order = []
    ts_seen = set()
    per_ts_sum = {}  # ts → [nb buckets cumulative sum across ops]
    for row in rows:
        ts = row[ts_i] if ts_i < len(row) else ""
        if ts not in ts_seen:
            ts_order.append(ts)
            ts_seen.add(ts)
            per_ts_sum[ts] = [0] * nb
        for b, ci in enumerate(lba_indices):
            if ci < len(row) and row[ci] not in ("", None):
                try:
                    per_ts_sum[ts][b] += int(float(row[ci]))
                except ValueError:
                    pass

    # 누적값이라 인터벌 delta = 현재 - 이전 (첫 인터벌은 그대로)
    deltas = []
    prev = [0] * nb
    for ts in ts_order:
        cur = per_ts_sum[ts]
        d = [max(0, cur[b] - prev[b]) for b in range(nb)]
        deltas.append(d)
        prev = cur

    return {"timestamps": ts_order, "buckets": deltas}


def _render_lba_heatmap(dname, header, rows):
    data = _build_lba_heatmap(header, rows)
    if not data or not any(any(row) for row in data["buckets"]):
        return ""
    safe = re.sub(r"[^a-zA-Z0-9]", "_", dname)
    payload_json = json.dumps(data, separators=(",", ":"))
    # bucket 수에 비례한 캔버스 높이 (cell당 ~3.5px 보장)
    ny = len(data["buckets"][0]) if data["buckets"] else 64
    canvas_h = max(320, 40 + ny * 4)
    return f"""<div class='heatmap-cell'>
<canvas id='heatmap_{safe}' width='720' height='{canvas_h}'></canvas>
<p class='meta'>LBA 분포 heatmap (bucket 0 = 디스크 앞부분, 마지막 = 뒷부분). 색: log10(인터벌 접근 횟수) — <span style='background:hsl(240,85%,50%);color:#fff;padding:0 4px;border-radius:2px'>차가움</span>→<span style='background:hsl(120,85%,45%);color:#fff;padding:0 4px;border-radius:2px'>중간</span>→<span style='background:hsl(0,85%,40%);color:#fff;padding:0 4px;border-radius:2px'>뜨거움</span>. 회색 = 접근 0.</p>
</div>
<script>(function() {{
const data = {payload_json};
const canvas = document.getElementById('heatmap_{safe}');
if (!canvas) return;
const ctx = canvas.getContext('2d');
const W = canvas.width, H = canvas.height;
const padL = 50, padB = 30, padT = 10, padR = 10;
const nx = data.timestamps.length || 1;
const ny = (data.buckets[0] || []).length || 64;
const cellW = (W - padL - padR) / nx;
const cellH = (H - padT - padB) / ny;
let mx = 0;
for (const col of data.buckets) for (const v of col) if (v > mx) mx = v;
const logMax = Math.log10(mx + 1) || 1;
// 빈 셀(=0) 명확히 보이게 진한 회색 배경
ctx.fillStyle = '#dcdee2'; ctx.fillRect(0, 0, W, H);
// 차트 영역만 더 짙은 회색 (라벨 영역과 구분)
ctx.fillStyle = '#cfd2d7'; ctx.fillRect(padL, padT, W - padL - padR, H - padT - padB);
for (let xi = 0; xi < nx; xi++) {{
  const col = data.buckets[xi];
  for (let yi = 0; yi < ny; yi++) {{
    const v = col[yi];
    if (v <= 0) continue;  // 회색 배경 그대로 두고 "값 있음"만 색칠
    const t = Math.log10(v + 1) / logMax;  // 0..1
    // 파랑(차가움) → 청록 → 노랑 → 빨강(뜨거움). HSL hue 240→0 회전 + 채도/명도 유지.
    // t=0: hue 240 deg (blue), t=1: hue 0 deg (red). 흰색 영역(L=100%) 회피.
    const hue = Math.round(240 * (1 - t));
    const sat = 85;
    const light = 50 - 10 * t;   // 더 뜨거울수록 약간 어둡게 → 채도 강조
    ctx.fillStyle = `hsl(${{hue}},${{sat}}%,${{light}}%)`;
    ctx.fillRect(padL + xi * cellW, padT + (ny - 1 - yi) * cellH, Math.ceil(cellW), Math.ceil(cellH));
  }}
}}
ctx.fillStyle = '#333'; ctx.font = '11px ui-monospace, monospace';
ctx.textAlign = 'right';
ctx.fillText('bucket ' + (ny - 1), padL - 4, padT + 10);
ctx.fillText('0', padL - 4, H - padB);
ctx.textAlign = 'center';
for (let xi = 0; xi < nx; xi++) {{
  if (xi % Math.max(1, Math.floor(nx / 8)) !== 0) continue;
  ctx.fillText(data.timestamps[xi], padL + xi * cellW + cellW / 2, H - padB + 14);
}}
ctx.fillStyle = '#666'; ctx.textAlign = 'left';
ctx.fillText(`max ${{mx.toLocaleString()}}`, padL, H - 4);
}})();</script>"""


def _render_multi_device_overview(device_csv_paths):
    """여러 device CSV를 한 패널에 overlay — 디바이스별 op-합산 IOPS/BW 시계열.
    1개 device면 자연스럽게 1 line, N개면 N lines (디바이스 비교 가능)."""
    if not device_csv_paths:
        return ""
    # device 단위 합계: 같은 timestamp에서 모든 op iops/bw 합
    per_dev = {}   # {dname: {ts: {'iops': N, 'bw': N}}}
    all_ts_order = []
    seen_ts = set()
    for dpath in device_csv_paths:
        h, r = _load_csv(dpath)
        if not h or not r:
            continue
        try:
            ts_i = h.index("timestamp")
            iops_i = h.index("iops_interval")
            bw_i = h.index("bandwidth_mb_s_interval")
        except ValueError:
            continue
        dn = os.path.basename(dpath)
        dn = re.sub(r"_\d{8}_\d{6}\.csv$", "", dn)
        bucket = per_dev.setdefault(dn, {})
        for row in r:
            ts = row[ts_i] if ts_i < len(row) else ""
            if not ts:
                continue
            if ts not in seen_ts:
                all_ts_order.append(ts); seen_ts.add(ts)
            cell = bucket.setdefault(ts, {"iops": 0.0, "bw": 0.0})
            try:
                cell["iops"] += float(row[iops_i] or 0)
                cell["bw"] += float(row[bw_i] or 0)
            except ValueError:
                pass
    if not per_dev:
        return ""

    series_iops = {dn: [per_dev[dn].get(t, {}).get("iops") for t in all_ts_order] for dn in per_dev}
    series_bw   = {dn: [per_dev[dn].get(t, {}).get("bw")   for t in all_ts_order] for dn in per_dev}
    payload = {"labels": all_ts_order, "iops": series_iops, "bw": series_bw}
    payload_json = json.dumps(payload, separators=(",", ":"))
    palette = json.dumps(_PALETTE)
    return f"""<h3>Overview (디바이스 비교)</h3>
<div class='chart-row'>
<div class='chart-cell'><canvas id='chart_multi_iops'></canvas></div>
<div class='chart-cell'><canvas id='chart_multi_bw'></canvas></div>
</div>
<script>(function() {{
if (typeof Chart === 'undefined') {{
  document.querySelectorAll('[id^="chart_multi_"]').forEach(c => c.parentNode.innerHTML = '<p class=\\'chart-warn\\'>Chart.js CDN unreachable.</p>');
  return;
}}
const p = {payload_json};
const palette = {palette};
[['chart_multi_iops','iops','IOPS (per device, ops/s)','ops/s'],
 ['chart_multi_bw','bw','Bandwidth (per device, MB/s)','MB/s']].forEach(function(spec) {{
  const cid = spec[0], metric = spec[1], title = spec[2], yl = spec[3];
  const ctx = document.getElementById(cid);
  if (!ctx) return;
  const devNames = Object.keys(p[metric]);
  const datasets = devNames.map(function(dn, i) {{
    return {{label: dn, data: p[metric][dn], borderColor: palette[i % palette.length],
             backgroundColor: 'transparent', pointRadius: 1, tension: 0.2, spanGaps: true}};
  }});
  new Chart(ctx.getContext('2d'), {{
    type: 'line', data: {{labels: p.labels, datasets: datasets}},
    options: {{responsive: true, maintainAspectRatio: false, animation: false,
               plugins: {{title: {{display: true, text: title}}, legend: {{position: 'bottom'}}}},
               scales: {{y: {{title: {{display: true, text: yl}}, beginAtZero: true}},
                         x: {{ticks: {{maxTicksLimit: 12}}}}}}}}
  }});
}});
}})();</script>"""


def _render_device_charts(dname, header, rows):
    labels, series = _build_device_series(header, rows)
    if not labels or not series:
        return ""
    safe = re.sub(r"[^a-zA-Z0-9]", "_", dname)
    # p50/p99 시리즈가 모두 None이면 단순 d2c avg chart만 표시 (구버전 CSV 호환).
    has_p = any(any(v is not None for v in (s.get("p50") or []) + (s.get("p99") or []))
                for s in series.values())
    charts = [
        ("iops", "IOPS",        "ops/s"),
        ("bw",   "Bandwidth",   "MB/s"),
    ]
    if has_p:
        # 가장 바쁜 op (총 iops 최대) 자동 선택해 avg/p50/p99 단일 차트로 통합
        top_op = max(series.keys(),
                     key=lambda k: sum((v or 0) for v in series[k].get("iops") or []),
                     default=None)
        charts.append(("lat", f"D2C latency (avg/p50/p99) — {top_op or 'top op'}", "us"))
    else:
        charts.append(("d2c", "D2C avg", "us"))
    parts = ["<div class='chart-row'>"]
    for metric, _, _ in charts:
        parts.append(f"<div class='chart-cell'><canvas id='chart_{safe}_{metric}'></canvas></div>")
    parts.append("</div>")

    top_op_for_lat = None
    if has_p:
        top_op_for_lat = max(series.keys(),
                              key=lambda k: sum((v or 0) for v in series[k].get("iops") or []),
                              default=None)
    payload = {
        "labels": labels,
        "series": series,
        "colors": _OP_COLORS,
        "top_op": top_op_for_lat,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    chart_specs = json.dumps(charts, separators=(",", ":"))
    parts.append(f"""<script>(function(){{
if (typeof Chart === 'undefined') {{
  document.querySelectorAll('[id^="chart_{safe}_"]').forEach(c => c.parentNode.innerHTML = '<p class=\\'chart-warn\\'>Chart.js CDN unreachable — see table above.</p>');
  return;
}}
const payload = {payload_json};
const charts = {chart_specs};
const metricColors = {{avg: '#0a84ff', p50: '#30d158', p99: '#ef4444'}};
charts.forEach(function(spec) {{
  const metric = spec[0], title = spec[1], ylabel = spec[2];
  let datasets;
  if (metric === 'lat' && payload.top_op) {{
    // single-op, 3 metric lines: avg/p50/p99 (consolidated latency view)
    const op = payload.top_op;
    const s = payload.series[op] || {{}};
    datasets = [
      {{label: 'd2c avg', data: s.d2c, borderColor: metricColors.avg, backgroundColor: 'transparent',
        pointRadius: 1, tension: 0.2, spanGaps: true}},
      {{label: 'd2c p50', data: s.p50, borderColor: metricColors.p50, borderDash: [4,2], backgroundColor: 'transparent',
        pointRadius: 1, tension: 0.2, spanGaps: true}},
      {{label: 'd2c p99', data: s.p99, borderColor: metricColors.p99, borderWidth: 2, backgroundColor: 'transparent',
        pointRadius: 1, tension: 0.2, spanGaps: true}}
    ];
  }} else {{
    datasets = Object.keys(payload.series).map(function(op) {{
      return {{label: op, data: payload.series[op][metric], borderColor: payload.colors[op] || '#888',
               backgroundColor: 'transparent', pointRadius: 1, tension: 0.2, spanGaps: true}};
    }});
  }}
  const cid = 'chart_{safe}_' + metric;
  const ctx = document.getElementById(cid);
  if (!ctx) return;
  new Chart(ctx.getContext('2d'), {{
    type: 'line', data: {{labels: payload.labels, datasets: datasets}},
    options: {{responsive: true, maintainAspectRatio: false, animation: false,
               plugins: {{title: {{display: true, text: title}}, legend: {{position: 'bottom'}}}},
               scales: {{y: {{title: {{display: true, text: ylabel}}, beginAtZero: true}},
                         x: {{ticks: {{maxTicksLimit: 12}}}}}}}}
  }});
}});
}})();</script>""")
    return "".join(parts)


def _render_topology_svg(topo):
    """간단 inline SVG: NUMA node 박스 + 그 아래 CPU range, NVMe/GPU 박스를
    소속 NUMA node에 라인으로 연결. 외부 라이브러리 0."""
    if not topo:
        return ""
    nodes = topo.get("nodes") or []
    if not nodes:
        return ""
    cpu_map = topo.get("cpu_to_node") or {}
    # topology.json의 raw는 flat (raw.nvme_ctrls). 구버전 호환을 위해 .discovered fallback도 시도.
    raw = topo.get("raw") or {}
    nvmes_full = raw.get("nvme_ctrls") or raw.get("discovered", {}).get("nvme_ctrls") or []
    nvme_names = topo.get("nvme_controllers") or []
    nvme_to_node = {}
    for c in nvmes_full:
        if c.get("name") in nvme_names:
            nvme_to_node[c["name"]] = c.get("numa_node", "-1")
    gpus = topo.get("gpus") or []

    # node→cpus 역매핑
    node_to_cpus = {}
    for cpu, node in cpu_map.items():
        node_to_cpus.setdefault(str(node), []).append(int(cpu))
    for n in node_to_cpus:
        node_to_cpus[n] = sorted(node_to_cpus[n])

    W = 760
    node_w = max(140, (W - 40) // max(1, len(nodes)))
    node_h = 56
    pad_top = 20
    y_node = pad_top
    y_dev = y_node + node_h + 70  # 디바이스(NVMe/GPU)는 아래 row
    h_dev = 36
    H = y_dev + h_dev + 30

    parts = [f"<svg width='{W}' height='{H}' xmlns='http://www.w3.org/2000/svg' style='font-family: ui-monospace, monospace; font-size: 11px;'>"]
    parts.append("<style>.nodebox{fill:#dbeafe;stroke:#1d4ed8;stroke-width:1.5} "
                 ".devbox{fill:#fef3c7;stroke:#b45309;stroke-width:1.5} "
                 ".gpubox{fill:#dcfce7;stroke:#15803d;stroke-width:1.5} "
                 ".lbl{fill:#1f2937} .sub{fill:#6b7280;font-size:10px} "
                 ".edge{stroke:#9ca3af;stroke-width:1;fill:none}</style>")

    node_centers = {}
    for i, node in enumerate(nodes):
        x = 20 + i * node_w
        cx = x + (node_w - 20) / 2
        node_centers[str(node)] = cx
        parts.append(f"<rect class='nodebox' x='{x}' y='{y_node}' width='{node_w - 20}' height='{node_h}' rx='6'/>")
        parts.append(f"<text class='lbl' x='{cx}' y='{y_node + 22}' text-anchor='middle'>NUMA node {node}</text>")
        cpus = node_to_cpus.get(str(node), [])
        parts.append(f"<text class='sub' x='{cx}' y='{y_node + 42}' text-anchor='middle'>CPU {_compact_cpu_list(cpus)} ({len(cpus)})</text>")

    # 디바이스 row: NVMe + GPU 함께. 균등 분할.
    devs = [("nvme", n, nvme_to_node.get(n, "-1")) for n in nvme_names]
    devs += [("gpu", g.get("name", "?"), str(g.get("numa_node", "-1"))) for g in gpus]
    if devs:
        dev_w = max(120, (W - 40) // max(1, len(devs)))
        for i, (kind, name, node_id) in enumerate(devs):
            x = 20 + i * dev_w
            cx = x + (dev_w - 20) / 2
            klass = "gpubox" if kind == "gpu" else "devbox"
            parts.append(f"<rect class='{klass}' x='{x}' y='{y_dev}' width='{dev_w - 20}' height='{h_dev}' rx='4'/>")
            short = name if len(name) < 22 else name[:19] + "..."
            parts.append(f"<text class='lbl' x='{cx}' y='{y_dev + 16}' text-anchor='middle'>{short}</text>")
            parts.append(f"<text class='sub' x='{cx}' y='{y_dev + 30}' text-anchor='middle'>{kind.upper()} · NUMA {node_id}</text>")
            # edge to its NUMA node (있을 때만)
            ncx = node_centers.get(str(node_id))
            if ncx is not None:
                parts.append(f"<line class='edge' x1='{cx}' y1='{y_dev}' x2='{ncx}' y2='{y_node + node_h}'/>")
    parts.append("</svg>")
    return "<div style='margin: 0.5em 0;'>" + "".join(parts) + "</div>"


def _render_topology(topo):
    if not topo:
        return "<p><em>topology.json 없음</em></p>"
    nodes = topo.get("nodes") or []
    nvmes = topo.get("nvme_controllers") or []
    gpus = topo.get("gpus") or []
    cpu_map = topo.get("cpu_to_node") or {}

    # node→cpus 역매핑
    node_to_cpus = {}
    for cpu, node in cpu_map.items():
        node_to_cpus.setdefault(str(node), []).append(int(cpu))
    for n in node_to_cpus:
        node_to_cpus[n] = sorted(node_to_cpus[n])

    out = [_render_topology_svg(topo), "<dl class='topology'>"]
    out.append(f"<dt>NUMA nodes</dt><dd>{html.escape(', '.join(map(str, nodes)) or '(none)')}</dd>")
    for node in nodes:
        cpus = node_to_cpus.get(str(node), [])
        out.append(f"<dt>node {html.escape(str(node))} CPUs</dt><dd><code>{html.escape(_compact_cpu_list(cpus))}</code> ({len(cpus)}개)</dd>")
    out.append(f"<dt>NVMe controllers</dt><dd>{html.escape(', '.join(nvmes) or '(none)')}</dd>")
    # nvme controller 상세 (topology.json의 raw.nvme_ctrls — flat 구조)
    raw = topo.get("raw") or {}
    ctrls = raw.get("nvme_ctrls") or raw.get("discovered", {}).get("nvme_ctrls") or []
    if ctrls:
        rows = []
        for c in ctrls:
            rows.append([
                c.get("name", "?"),
                c.get("model", "?").strip(),
                c.get("firmware_rev", "?"),
                str(c.get("queue_count", "?")),
                c.get("state", "?"),
                c.get("transport", "?"),
                c.get("numa_node", "?"),
            ])
        ctrl_table = _render_table(["ctrl", "model", "firmware", "queue_count", "state", "transport", "numa"], rows)
        out.append(f"<dt>NVMe details</dt><dd>{ctrl_table}</dd>")
    if gpus:
        gpu_descs = []
        for g in gpus:
            idx = g.get("index", "?")
            name = g.get("name", "?")
            numa = g.get("numa_node", "?")
            gpu_descs.append(f"#{idx} {name} (NUMA {numa})")
        out.append(f"<dt>GPUs</dt><dd>{html.escape(' / '.join(gpu_descs))}</dd>")
    else:
        out.append("<dt>GPUs</dt><dd>(none)</dd>")
    out.append("</dl>")
    return "".join(out)


def _compact_cpu_list(cpus):
    """[0,1,2,3,7,8] → '0-3,7-8'."""
    if not cpus:
        return ""
    cpus = sorted(cpus)
    ranges = []
    a = b = cpus[0]
    for c in cpus[1:]:
        if c == b + 1:
            b = c
        else:
            ranges.append(f"{a}-{b}" if a != b else f"{a}")
            a = b = c
    ranges.append(f"{a}-{b}" if a != b else f"{a}")
    return ",".join(ranges)


HTML_CSS = """
  body { font-family: -apple-system, "Segoe UI", "Noto Sans", sans-serif; margin: 1em 2em; color: #222; }
  h1 { border-bottom: 2px solid #333; padding-bottom: 0.2em; }
  h2 { margin-top: 2em; border-bottom: 1px solid #888; }
  table { border-collapse: collapse; font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 0.85em; margin: 0.5em 0; max-width: 100%; overflow-x: auto; display: block; }
  th, td { border: 1px solid #ccc; padding: 0.2em 0.4em; text-align: right; white-space: nowrap; }
  th { background: #eef; position: sticky; top: 0; }
  tr:nth-child(even) td { background: #f7f7f7; }
  dl.topology dt { font-weight: bold; }
  dl.topology dd { margin-left: 1em; margin-bottom: 0.3em; }
  .note { color: #888; font-size: 0.85em; }
  code { background: #eee; padding: 0.1em 0.3em; border-radius: 3px; }
  .meta { color: #666; font-size: 0.9em; }
  .summary-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.6em; margin: 1em 0; }
  .card { background: #f0f4f9; border-left: 3px solid #1d4ed8; padding: 0.5em 0.8em; border-radius: 3px; }
  .card .v { font-size: 1.4em; font-weight: bold; color: #111; font-family: ui-monospace, monospace; }
  .card .l { color: #555; font-size: 0.85em; }
  .card .h { color: #999; font-size: 0.75em; font-style: italic; }
  .card.warn { border-left-color: #f59e0b; background: #fffaf0; }
  .card.bad  { border-left-color: #ef4444; background: #fef2f2; }
  .findings { background: #f9fafb; border: 1px solid #e5e7eb; padding: 0.6em 1em; border-radius: 4px; margin: 0.8em 0; }
  .findings li { margin: 0.25em 0; }
  .chart-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 1em; margin: 1em 0; }
  .chart-cell { background: #fafafa; border: 1px solid #ddd; padding: 0.5em; height: 280px; position: relative; }
  .chart-warn { color: #b00; font-style: italic; }
  .heatmap-cell { margin: 1em 0; }
  .heatmap-cell canvas { border: 1px solid #ccc; max-width: 100%; height: auto; }
"""


CHART_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4"


def _html_head(sid, now, src):
    return (
        "<!doctype html>\n<html lang='ko'><head><meta charset='utf-8'>\n"
        f"<title>perf report {html.escape(sid)}</title>\n"
        f"<style>{HTML_CSS}</style>\n"
        f"<script src='{CHART_CDN}'></script>\n"
        "</head><body>\n"
        f"<h1>Performance Report — session {html.escape(sid)}</h1>\n"
        f"<p class='meta'>Generated {html.escape(now)} · source: <code>{html.escape(src)}</code></p>\n"
    )

HTML_FOOT = "</body></html>\n"


def build_report(session_dir, sid):
    topo = _load_topology(session_dir, sid)
    sys_path = os.path.join(session_dir, f"system_metrics_{sid}.csv")
    device_csvs = sorted(glob.glob(os.path.join(session_dir, f"*_{sid}.csv")))
    # system_metrics_*.csv 는 device 그룹과 분리
    device_csvs = [p for p in device_csvs if not os.path.basename(p).startswith("system_metrics_")]

    parts = [_html_head(sid, datetime.now().isoformat(timespec="seconds"), os.path.abspath(session_dir))]

    # Executive summary (카드 + Top findings) — 모든 차트보다 위.
    try:
        from .md_report import _device_aggregates, _system_aggregates
        sys_h_tmp, sys_r_tmp = _load_csv(sys_path)
        sys_agg = _system_aggregates(sys_h_tmp, sys_r_tmp) if sys_h_tmp else {}
        dev_aggs = {}
        for dp in device_csvs:
            dh, dr = _load_csv(dp)
            if dh:
                dn = re.sub(r"_\d{8}_\d{6}\.csv$", "", os.path.basename(dp))
                dev_aggs[dn] = _device_aggregates(dh, dr)
        parts.append(_render_summary(dev_aggs, sys_agg))
        # I/O × System 상관 차트 — summary 직후, 어느 섹션보다 위.
        parts.append("<h2>I/O × System correlation</h2>")
        parts.append("<p class='meta'>IOPS 변화와 CPU iowait/sys% 변화를 같은 시간축에서 본다. dual y-axis (왼쪽: IOPS, 오른쪽: %).</p>")
        parts.append(_render_correlation_chart(sys_h_tmp, sys_r_tmp, device_csvs))
    except Exception as e:
        parts.append(f"<p class='meta'>(summary 생성 실패: {html.escape(str(e))})</p>")

    parts.append("<h2>1. Topology</h2>")
    parts.append(_render_topology(topo))

    parts.append("<h2>2. System metrics (per-second)</h2>")
    h, r = _load_csv(sys_path)
    if h is None:
        parts.append("<p><em>system_metrics CSV 없음</em></p>")
    else:
        parts.append(f"<p class='meta'>{len(r)}행 × {len(h)}컬럼 · 원본: <code>{html.escape(os.path.basename(sys_path))}</code></p>")
        sys_payload = _build_system_series(h, r)
        parts.append(_render_system_charts(sys_payload))
        parts.append(_render_table(h, r))

    parts.append("<h2>3. Device I/O metrics</h2>")
    if not device_csvs:
        parts.append("<p><em>device CSV 없음</em></p>")
    else:
        parts.append(_render_multi_device_overview(device_csvs))
        for dpath in device_csvs:
            dname = os.path.basename(dpath)
            parts.append(f"<h3>{html.escape(dname)}</h3>")
            h, r = _load_csv(dpath)
            parts.append(f"<p class='meta'>{len(r)}행 × {len(h)}컬럼</p>")
            parts.append(_render_device_charts(dname, h, r))
            parts.append(_render_lba_heatmap(dname, h, r))
            parts.append(_render_table(h, r))

    parts.append(HTML_FOOT)
    return "".join(parts)


def main(argv=None):
    p = argparse.ArgumentParser(description="세션 산출물을 자기완결 HTML 리포트로 변환")
    p.add_argument("--session-dir", default="ebpf/csv_results",
                   help="세션 CSV/JSON 산출물 디렉터리 (기본: ebpf/csv_results)")
    p.add_argument("--session-id", default=None,
                   help="세션 ID (YYYYMMDD_HHMMSS). 생략 시 가장 최근 자동 선택")
    p.add_argument("-o", "--output", default=None,
                   help="출력 HTML 경로 (기본: <session-dir>/report_<sid>.html)")
    args = p.parse_args(argv)

    sd = args.session_dir
    if not os.path.isdir(sd):
        print(f"[!] 세션 디렉터리 없음: {sd}", file=sys.stderr)
        return 2
    sid = args.session_id or _discover_session(sd)
    if not sid:
        print(f"[!] topology_*.json 없음 in {sd}", file=sys.stderr)
        return 2

    out = args.output or os.path.join(sd, f"report_{sid}.html")
    html_str = build_report(sd, sid)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_str)
    print(f"[report] wrote {out} ({len(html_str)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
