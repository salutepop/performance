"""
Shared report data helpers — session discovery, CSV/topology loading, and
chart-series builders. Used by report.png_report / md_report / summary.

Pure data layer: no rendering, no output-format assumptions.
"""

import csv
import glob
import json
import os

def _discover_session(session_dir):
    """가장 최근 topology_<sid>.json 의 session_id 반환. 없으면 None.

    sid는 더 이상 고정 타임스탬프가 아니므로 (라벨이 붙을 수 있음)
    `topology_` 와 `.json` 사이를 그대로 slice 한다.
    """
    paths = sorted(glob.glob(os.path.join(session_dir, "topology_*.json")), reverse=True)
    if not paths:
        return None
    base = os.path.basename(paths[0])
    if base.startswith("topology_") and base.endswith(".json"):
        return base[len("topology_"):-len(".json")]
    return None


def _load_topology(session_dir, sid):
    p = os.path.join(session_dir, f"topology_{sid}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def _dev_name(csv_path):
    """Device CSV is `{device}_{sid}.csv` -> return the bare `{device}`.

    A block device name never contains '_' (nvme0n1, sda, ...) and the sid
    follows the first '_', so split on it. Robust to labelled session ids,
    unlike the old `_\\d{8}_\\d{6}.csv$` regex.
    """
    return os.path.basename(csv_path).split("_", 1)[0]


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


def _build_lba_heatmap(header, rows, op_filter=None):
    """device CSV → {timestamps:[], buckets: [[delta per bucket] per ts]}.

    op_filter: set of operation names to include (None = all ops).
    lba_N 컬럼은 op별 누적값 — op별로 인터벌 delta를 구한 뒤 timestamp 단위로
    합산한다 (delta-then-sum). op이 인터벌마다 등장/소멸해도 안전.
    """
    if not header or not rows:
        return None
    try:
        ts_i = header.index("timestamp")
        op_i = header.index("operation")
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

    ts_order = []
    ts_seen = set()
    per_ts_delta = {}  # ts → [nb] interval delta summed over the filtered ops
    prev = {}          # op → [nb] cumulative
    for row in rows:
        ts = row[ts_i] if ts_i < len(row) else ""
        op = row[op_i] if op_i < len(row) else "?"
        if ts not in ts_seen:
            ts_order.append(ts)
            ts_seen.add(ts)
            per_ts_delta[ts] = [0] * nb
        if op_filter is not None and op not in op_filter:
            continue
        cur = []
        for ci in lba_indices:
            v = 0
            if ci < len(row) and row[ci] not in ("", None):
                try:
                    v = int(float(row[ci]))
                except ValueError:
                    v = 0
            cur.append(v)
        p = prev.get(op, [0] * nb)
        for b in range(nb):
            per_ts_delta[ts][b] += max(0, cur[b] - p[b])
        prev[op] = cur

    return {"timestamps": ts_order, "buckets": [per_ts_delta[t] for t in ts_order]}
