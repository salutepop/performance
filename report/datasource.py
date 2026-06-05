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


def _timestamp_window(csv_paths):
    """주어진 CSV들의 `timestamp` 컬럼을 통틀어 [first, last] 윈도우 반환.

    timestamp는 "HH:MM:SS" 문자열이라 세션 범위 안에서는 lexical min/max가
    곧 시간순. 행이 있는 CSV가 하나도 없으면 (None, None)."""
    firsts, lasts = [], []
    for p in csv_paths:
        h, r = _load_csv(p)
        if not h or not r:
            continue
        try:
            ti = h.index("timestamp")
        except ValueError:
            continue
        ts = [row[ti] for row in r if ti < len(row) and row[ti]]
        if ts:
            firsts.append(min(ts))
            lasts.append(max(ts))
    if not firsts:
        return None, None
    return min(firsts), max(lasts)


def _crop_rows(header, rows, t0, t1):
    """`timestamp`가 [t0, t1] 범위에 드는 행만 남긴다."""
    if not header or rows is None or t0 is None:
        return rows
    try:
        ti = header.index("timestamp")
    except ValueError:
        return rows
    return [r for r in rows if ti < len(r) and t0 <= r[ti] <= t1]

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


def _build_overview_series(sys_header, sys_rows, device_csv_paths):
    """단일 리소스 오버뷰 차트용 시리즈 — Total CPU %, 사용 메모리, 디바이스별
    bandwidth를 system_metrics 타임라인 위에 한 번에 묶는다.

    반환 {labels, cpu:[%], mem:[GB], mem_label, bw:{dev:[MB/s]}} 또는 None.

    - cpu: 노드별 (user+sys+iowait+irq+softirq)를 노드 수로 평균 → 시스템 전체
      busy %. 각 node_*_pct는 그 노드 jiffies 기준 비율이라 노드 평균이 곧
      시스템 전체 비율.
    - mem: per-NUMA used MB 합을 GB로. NUMA meminfo가 없으면 MemAvailable로
      폴백(라벨도 그에 맞게 바뀐다).
    - bw: 디바이스 CSV의 bandwidth_mb_s_interval을 op 합산 후 labels로 reindex."""
    if not sys_header or not sys_rows:
        return None
    try:
        ts_i = sys_header.index("timestamp")
    except ValueError:
        return None
    labels = [r[ts_i] for r in sys_rows if ts_i < len(r)]

    def _f(row, col):
        i = sys_header.index(col)
        if i < len(row) and row[i] not in ("", None):
            try:
                return float(row[i])
            except ValueError:
                return 0.0
        return 0.0

    # Total CPU% — 노드별 busy 합을 노드 수로 평균
    cpu_cols = [c for c in sys_header if c.startswith("node") and (
        c.endswith("_user_pct") or c.endswith("_sys_pct")
        or c.endswith("_iowait_pct") or c.endswith("_irq_pct")
        or c.endswith("_softirq_pct"))]
    nodes = sorted({c.split("_")[0] for c in cpu_cols})
    cpu = []
    for row in sys_rows:
        if not nodes:
            cpu.append(None)
            continue
        per_node = [sum(_f(row, c) for c in cpu_cols if c.startswith(nd + "_"))
                    for nd in nodes]
        cpu.append(sum(per_node) / len(per_node))

    # Memory — per-NUMA used 합(GB), 없으면 MemAvailable(GB)
    used_cols = [c for c in sys_header
                 if c.startswith("node") and c.endswith("_mem_used_mb")]
    if used_cols:
        mem_label = "Mem used [GB]"
        mem = [sum(_f(row, c) for c in used_cols) / 1024.0 for row in sys_rows]
    elif "mem_available_mb" in sys_header:
        mem_label = "Mem avail [GB]"
        mem = [_f(row, "mem_available_mb") / 1024.0 for row in sys_rows]
    else:
        mem_label, mem = None, []

    # Per-device bandwidth [MB/s] — op 합산 후 labels로 reindex
    bw = {}
    for dpath in device_csv_paths or []:
        h, r = _load_csv(dpath)
        if not h:
            continue
        try:
            dts_i = h.index("timestamp")
            bw_i = h.index("bandwidth_mb_s_interval")
        except ValueError:
            continue
        bucket = {}
        for row in r:
            ts = row[dts_i] if dts_i < len(row) else ""
            try:
                v = (float(row[bw_i])
                     if bw_i < len(row) and row[bw_i] not in ("", None) else 0.0)
            except ValueError:
                v = 0.0
            bucket[ts] = bucket.get(ts, 0.0) + v
        bw[_dev_name(dpath)] = [bucket.get(t) for t in labels]

    return {"labels": labels, "cpu": cpu, "mem": mem,
            "mem_label": mem_label, "bw": bw}


def _build_device_series(header, rows, timeline=None):
    """device CSV → (labels[], series{op:{iops,bw,d2c,p50,p99,q2d,q2d_p99}}). 모든 op timestamp 통합·정렬.

    timeline: 주입된 마스터 타임라인. 주면 그 위로 reindex (결손 인터벌은 None),
    없으면 device CSV 자체 timestamp 순서를 쓴다."""
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
    q2d_i = header.index("q2d_avg_us_interval") if "q2d_avg_us_interval" in header else -1
    q2d_p99_i = header.index("q2d_p99_us") if "q2d_p99_us" in header else -1
    cqd_i = header.index("current_qd") if "current_qd" in header else -1
    mqd_i = header.index("max_qd") if "max_qd" in header else -1

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
            "q2d":     _f(row[q2d_i])     if 0 <= q2d_i < len(row) else None,
            "q2d_p99": _f(row[q2d_p99_i]) if 0 <= q2d_p99_i < len(row) else None,
            "current_qd": _f(row[cqd_i]) if 0 <= cqd_i < len(row) else None,
            "max_qd":     _f(row[mqd_i]) if 0 <= mqd_i < len(row) else None,
        }
    out = timeline if timeline is not None else labels
    series = {}
    for op, by_ts in op_data.items():
        series[op] = {
            "iops": [by_ts.get(t, {}).get("iops") for t in out],
            "bw":   [by_ts.get(t, {}).get("bw")   for t in out],
            "d2c":  [by_ts.get(t, {}).get("d2c")  for t in out],
            "p50":  [by_ts.get(t, {}).get("p50")  for t in out],
            "p99":  [by_ts.get(t, {}).get("p99")  for t in out],
            "q2d":     [by_ts.get(t, {}).get("q2d")     for t in out],
            "q2d_p99": [by_ts.get(t, {}).get("q2d_p99") for t in out],
            "current_qd": [by_ts.get(t, {}).get("current_qd") for t in out],
            "max_qd":     [by_ts.get(t, {}).get("max_qd")     for t in out],
        }
    return out, series


def _build_lba_heatmap(header, rows, op_filter=None, timeline=None):
    """device CSV → {timestamps:[], buckets: [[delta per bucket] per ts]}.

    op_filter: set of operation names to include (None = all ops).
    timeline: 주입된 마스터 타임라인. 주면 그 위로 reindex (결손 인터벌은 0),
    없으면 device CSV 자체 timestamp 순서를 쓴다.
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

    out = timeline if timeline is not None else ts_order
    return {"timestamps": out,
            "buckets": [per_ts_delta.get(t, [0] * nb) for t in out]}
