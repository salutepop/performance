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


def _build_device_series(header, rows):
    """device CSV → (labels[], series{op:{iops,bw,d2c}}). 모든 op timestamp 통합·정렬."""
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
        }
    series = {}
    for op, by_ts in op_data.items():
        series[op] = {
            "iops": [by_ts.get(t, {}).get("iops") for t in labels],
            "bw":   [by_ts.get(t, {}).get("bw")   for t in labels],
            "d2c":  [by_ts.get(t, {}).get("d2c")  for t in labels],
        }
    return labels, series


def _render_device_charts(dname, header, rows):
    labels, series = _build_device_series(header, rows)
    if not labels or not series:
        return ""
    safe = re.sub(r"[^a-zA-Z0-9]", "_", dname)
    charts = [
        ("iops", "IOPS",      "ops/s"),
        ("bw",   "Bandwidth", "MB/s"),
        ("d2c",  "D2C Latency", "us"),
    ]
    parts = ["<div class='chart-row'>"]
    for metric, _, _ in charts:
        parts.append(f"<div class='chart-cell'><canvas id='chart_{safe}_{metric}'></canvas></div>")
    parts.append("</div>")

    payload = {
        "labels": labels,
        "series": series,
        "colors": _OP_COLORS,
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
charts.forEach(function(spec) {{
  const metric = spec[0], title = spec[1], ylabel = spec[2];
  const datasets = Object.keys(payload.series).map(function(op) {{
    return {{label: op, data: payload.series[op][metric], borderColor: payload.colors[op] || '#888',
             backgroundColor: 'transparent', pointRadius: 1, tension: 0.2, spanGaps: true}};
  }});
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

    out = ["<dl class='topology'>"]
    out.append(f"<dt>NUMA nodes</dt><dd>{html.escape(', '.join(map(str, nodes)) or '(none)')}</dd>")
    for node in nodes:
        cpus = node_to_cpus.get(str(node), [])
        out.append(f"<dt>node {html.escape(str(node))} CPUs</dt><dd><code>{html.escape(_compact_cpu_list(cpus))}</code> ({len(cpus)}개)</dd>")
    out.append(f"<dt>NVMe controllers</dt><dd>{html.escape(', '.join(nvmes) or '(none)')}</dd>")
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
  .chart-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 1em; margin: 1em 0; }
  .chart-cell { background: #fafafa; border: 1px solid #ddd; padding: 0.5em; height: 280px; position: relative; }
  .chart-warn { color: #b00; font-style: italic; }
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
        for dpath in device_csvs:
            dname = os.path.basename(dpath)
            parts.append(f"<h3>{html.escape(dname)}</h3>")
            h, r = _load_csv(dpath)
            parts.append(f"<p class='meta'>{len(r)}행 × {len(h)}컬럼</p>")
            parts.append(_render_device_charts(dname, h, r))
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
