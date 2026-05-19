import json
import subprocess
import os
import sys
import signal
import time
import argparse
import threading
import csv
from datetime import datetime

# sysmon은 프로젝트 루트의 core/ 모듈에 있음 — 상대 import 가능하도록 path 보정
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

try:
    from core.monitor import SystemMonitor
except Exception as _e:
    SystemMonitor = None
    print(f"[!] SystemMonitor import 실패: {_e} — system_metrics 수집은 비활성화")

# 산출물 위치는 cwd 무관 — io_profiler.py 가 있는 ebpf/ 디렉터리 기준 절대경로.
# 이전엔 "./csv_results" 상대 경로라 호출 cwd에 따라 다른 디렉터리에 떨어짐.
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csv_results")
SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

prev_metrics = {}
csv_buffers = {}
prev_libaio = {}  # {key: count or total_ns}, per-interval delta 계산용 (u2q_count/lat, c2a_*, a2u_*)
prev_sqcq = {}    # {dev_name: (same, diff)}, SQ↔CQ 일치 카운터의 인터벌 delta 계산용
prev_hists = {}   # {(dev,op,'q2d'|'d2c'): [32 buckets]} — 인터벌 히스토그램 delta 계산


# BPF op 이름 → libaio_overhead 필드 prefix 매핑. read_ahead/discard는 libaio 경로가 없어 None.
_LIBAIO_OP_KEY = {
    "read": "read",
    "write": "write",
    "flush": "flush",
}


LAT_HIST_BUCKETS = 32
LBA_BUCKETS = 128  # MUST match ebpf/io_trace.h. 변경 시 BPF 재빌드 필요.


def compute_percentiles(hist, pcts=(50, 95, 99, 99.9)):
    """log2(ns) histogram → {p: us}. 빈 히스토그램이면 None.
    bucket b는 [2^b, 2^(b+1)) ns 범위. 누적합 기준으로 선형 보간."""
    total = sum(hist) if hist else 0
    if total == 0:
        return {p: None for p in pcts}
    out = {}
    pct_queue = sorted(pcts)
    cum = 0
    pi = 0
    for b, count in enumerate(hist):
        prev_cum = cum
        cum += count
        while pi < len(pct_queue) and cum >= total * pct_queue[pi] / 100.0:
            target = total * pct_queue[pi] / 100.0
            if count > 0:
                frac = (target - prev_cum) / count
                lat_ns = (2 ** b) * (1 + frac)
            else:
                lat_ns = 2 ** b
            out[pct_queue[pi]] = lat_ns / 1000.0  # us
            pi += 1
    while pi < len(pct_queue):
        out[pct_queue[pi]] = (2 ** (LAT_HIST_BUCKETS - 1)) / 1000.0
        pi += 1
    return out


def _fmt_us(v):
    if v is None:
        return "    -  "
    if v >= 1000:
        return f"{v/1000:.2f}ms"
    return f"{v:.2f}us"


# 모니터링 제외 디바이스 prefix (가상/loop/ramdisk 등 — 분석 노이즈).
_EXCLUDED_DEV_PREFIXES = ("loop", "ram", "zram", "dm-", "md")


def _is_monitored_dev(name):
    """이름이 loop/ram/dm-/md 등 가상 디바이스가 아니면 True."""
    if not name:
        return False
    base = name.split("/")[-1]
    return not base.startswith(_EXCLUDED_DEV_PREFIXES)


def get_real_dev_name(dev_id_str):
    try:
        maj_min = dev_id_str.replace("dev(", "").replace(")", "")
        sysfs_path = f"/sys/dev/block/{maj_min}"
        if os.path.exists(sysfs_path):
            return os.path.basename(os.path.realpath(sysfs_path))
    except Exception:
        pass
    return dev_id_str.replace("(", "_").replace(")", "_").replace(":", "_")


def save_csv_buffers():
    global csv_buffers
    if not csv_buffers:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    keys = [
        "timestamp",
        "operation",
        "iops_interval",
        "bandwidth_mb_s_interval",
        "q2d_avg_us_interval",
        "d2c_avg_us_interval",
        "u2q_avg_us_interval",
        "c2a_avg_us_interval",
        "a2u_avg_us_interval",
        "sq_cq_diff_ratio",
        "d2c_p50_us",
        "d2c_p99_us",
        "q2d_p99_us",
        "current_qd",
        "max_qd",
        "total_io_count",
        "total_bytes",
        "q2d_total_ns",
        "q2d_min_ns",
        "q2d_max_ns",
        "d2c_total_ns",
        "d2c_min_ns",
        "d2c_max_ns",
        "size_hist_4k",
        "size_hist_32k",
        "size_hist_128k",
        "size_hist_large",
    ] + [f"lba_{i}" for i in range(LBA_BUCKETS)]

    for dev_name, rows in csv_buffers.items():
        if not rows:
            continue

        filename = os.path.join(OUTPUT_DIR, f"{dev_name}_{SESSION_ID}.csv")
        file_exists = os.path.isfile(filename)

        try:
            with open(filename, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                if not file_exists:
                    writer.writeheader()
                writer.writerows(rows)
            rows.clear()
        except Exception as e:
            print(f" [-] Failed to save CSV for {dev_name}: {e}")


def parse_and_store_metrics(json_str):
    global prev_metrics, csv_buffers, prev_libaio, prev_hists
    try:
        bpf_data = json.loads(json_str)
        timestamp = datetime.now().strftime("%H:%M:%S")

        # libaio_overhead 누적값을 인터벌 delta로 변환 (avg us 계산).
        sys_st = bpf_data.get("libaio_overhead", {}) or {}

        def _delta_avg_us(prefix):
            """prefix='c2a_read' → (delta_total_ns / delta_count) us 반환. 데이터 없으면 0."""
            cnt_k = f"{prefix}_count"
            tot_k = f"{prefix}_total" if prefix == "u2q_lat" else f"{prefix}_total"
            curr_c = sys_st.get(cnt_k, 0)
            curr_t = sys_st.get(tot_k, 0)
            prev_c = prev_libaio.get(cnt_k, 0)
            prev_t = prev_libaio.get(tot_k, 0)
            prev_libaio[cnt_k] = curr_c
            prev_libaio[tot_k] = curr_t
            dc = curr_c - prev_c
            dt = curr_t - prev_t
            return (dt / dc / 1000.0) if dc > 0 else 0.0

        # u2q는 op 구분 없는 글로벌 값 (모든 행에 같은 값 들어감).
        u2q_curr_c = sys_st.get("u2q_count", 0)
        u2q_curr_t = sys_st.get("u2q_lat_total", 0)
        u2q_prev_c = prev_libaio.get("u2q_count", 0)
        u2q_prev_t = prev_libaio.get("u2q_lat_total", 0)
        prev_libaio["u2q_count"] = u2q_curr_c
        prev_libaio["u2q_lat_total"] = u2q_curr_t
        u2q_dc = u2q_curr_c - u2q_prev_c
        u2q_dt = u2q_curr_t - u2q_prev_t
        u2q_avg_us_interval = (u2q_dt / u2q_dc / 1000.0) if u2q_dc > 0 else 0.0

        # op별 c2a/a2u avg us delta 미리 계산해두기
        op_libaio_avg = {}
        for bpf_op, lib_key in _LIBAIO_OP_KEY.items():
            op_libaio_avg[bpf_op] = {
                "c2a": _delta_avg_us(f"c2a_{lib_key}"),
                "a2u": _delta_avg_us(f"a2u_{lib_key}"),
            }

        for dev in bpf_data.get("devices", []):
            dev_name_raw = dev["dev_name"]
            real_name = get_real_dev_name(dev_name_raw)
            if not _is_monitored_dev(real_name):
                continue  # loop/ram/dm-/md 등 가상 디바이스 제외

            if real_name not in csv_buffers:
                csv_buffers[real_name] = []

            # SQ/CQ divergence delta (디바이스 단위, 인터벌 내 비율로 변환).
            sqcq = dev.get("sqcq", {}) or {}
            curr_same = sqcq.get("same", 0)
            curr_diff = sqcq.get("diff", 0)
            prev_same, prev_diff = prev_sqcq.get(real_name, (0, 0))
            prev_sqcq[real_name] = (curr_same, curr_diff)
            ds = max(0, curr_same - prev_same)
            dd = max(0, curr_diff - prev_diff)
            sq_cq_diff_ratio = (dd / (ds + dd)) if (ds + dd) > 0 else 0.0

            for op, stats in dev.get("operations", {}).items():
                curr_count = stats.get("total_count", 0)
                if curr_count == 0:
                    continue

                key = f"{real_name}_{op}"
                curr_bytes = stats.get("total_bytes", 0)
                curr_q2d_tot = stats.get("q2d", {}).get("total_lat_ns", 0)
                curr_d2c_tot = stats.get("d2c", {}).get("total_lat_ns", 0)

                prev = prev_metrics.get(
                    key, {"count": 0, "bytes": 0, "q2d_tot": 0, "d2c_tot": 0}
                )

                delta_count = curr_count - prev["count"]
                delta_bytes = curr_bytes - prev["bytes"]
                delta_q2d = curr_q2d_tot - prev["q2d_tot"]
                delta_d2c = curr_d2c_tot - prev["d2c_tot"]

                iops = delta_count
                bw_mb = delta_bytes / (1024.0 * 1024.0)
                q2d_avg_us = (
                    (delta_q2d / delta_count / 1000.0) if delta_count > 0 else 0.0
                )
                d2c_avg_us = (
                    (delta_d2c / delta_count / 1000.0) if delta_count > 0 else 0.0
                )

                current_qd = stats.get("current_qd", 0)
                max_qd = stats.get("max_qd", 0)

                if delta_count > 0:
                    print(
                        f" [{timestamp}] {real_name:<10} {op:<7} | IOPS: {iops:>6,} | BW: {bw_mb:>8.2f} MB/s | QD: {current_qd:>4} (Max: {max_qd:>4})"
                    )

                size_hist = stats.get("size_hist", [0, 0, 0, 0])
                lba_hist = stats.get("lba_hist", [0] * LBA_BUCKETS)

                op_libaio = op_libaio_avg.get(op, {"c2a": 0.0, "a2u": 0.0})

                # 인터벌 히스토그램 delta → 백분위 (32-bucket log2(ns))
                curr_q2d_hist = stats.get("q2d_hist") or [0] * LAT_HIST_BUCKETS
                curr_d2c_hist = stats.get("d2c_hist") or [0] * LAT_HIST_BUCKETS
                pkey_q = (real_name, op, "q2d")
                pkey_d = (real_name, op, "d2c")
                prev_q = prev_hists.get(pkey_q, [0] * LAT_HIST_BUCKETS)
                prev_d = prev_hists.get(pkey_d, [0] * LAT_HIST_BUCKETS)
                delta_q = [max(0, curr_q2d_hist[i] - prev_q[i]) for i in range(LAT_HIST_BUCKETS)]
                delta_d = [max(0, curr_d2c_hist[i] - prev_d[i]) for i in range(LAT_HIST_BUCKETS)]
                prev_hists[pkey_q] = curr_q2d_hist
                prev_hists[pkey_d] = curr_d2c_hist
                q2d_pcts = compute_percentiles(delta_q, (99,))
                d2c_pcts = compute_percentiles(delta_d, (50, 99))

                row = {
                    "timestamp": timestamp,
                    "operation": op,
                    "iops_interval": iops,
                    "bandwidth_mb_s_interval": round(bw_mb, 4),
                    "q2d_avg_us_interval": round(q2d_avg_us, 2),
                    "d2c_avg_us_interval": round(d2c_avg_us, 2),
                    "u2q_avg_us_interval": round(u2q_avg_us_interval, 2),
                    "c2a_avg_us_interval": round(op_libaio["c2a"], 2),
                    "a2u_avg_us_interval": round(op_libaio["a2u"], 2),
                    "sq_cq_diff_ratio": round(sq_cq_diff_ratio, 4),
                    "d2c_p50_us": round(d2c_pcts[50], 2) if d2c_pcts.get(50) is not None else None,
                    "d2c_p99_us": round(d2c_pcts[99], 2) if d2c_pcts.get(99) is not None else None,
                    "q2d_p99_us": round(q2d_pcts[99], 2) if q2d_pcts.get(99) is not None else None,
                    "current_qd": current_qd,
                    "max_qd": max_qd,
                    "total_io_count": curr_count,
                    "total_bytes": curr_bytes,
                    "q2d_total_ns": curr_q2d_tot,
                    "q2d_min_ns": stats.get("q2d", {}).get("min_lat_ns", 0),
                    "q2d_max_ns": stats.get("q2d", {}).get("max_lat_ns", 0),
                    "d2c_total_ns": curr_d2c_tot,
                    "d2c_min_ns": stats.get("d2c", {}).get("min_lat_ns", 0),
                    "d2c_max_ns": stats.get("d2c", {}).get("max_lat_ns", 0),
                    "size_hist_4k": size_hist[0],
                    "size_hist_32k": size_hist[1],
                    "size_hist_128k": size_hist[2],
                    "size_hist_large": size_hist[3],
                }

                for i, lba_val in enumerate(lba_hist):
                    row[f"lba_{i}"] = lba_val

                csv_buffers[real_name].append(row)

                prev_metrics[key] = {
                    "count": curr_count,
                    "bytes": curr_bytes,
                    "q2d_tot": curr_q2d_tot,
                    "d2c_tot": curr_d2c_tot,
                }

    except json.JSONDecodeError:
        pass


def print_op_stats(op_name, bpf_stats, c2a_data, a2u_data, duration):
    c2a_cnt, c2a_ms = c2a_data if c2a_data else (0, 0)
    a2u_cnt, a2u_ms = a2u_data if a2u_data else (0, 0)

    if bpf_stats.get("total_count", 0) == 0:
        return 0, 0, 0, 0, 0

    cnt = bpf_stats["total_count"]
    bpf_bytes = bpf_stats.get("total_bytes", 0)
    bpf_bw_mb = (bpf_bytes / (1024.0 * 1024.0)) / duration if duration > 0 else 0

    q2d_ms = bpf_stats.get("q2d", {}).get("total_lat_ns", 0) / 1000000.0
    d2c_ms = bpf_stats.get("d2c", {}).get("total_lat_ns", 0) / 1000000.0

    q2d_avg_us = (q2d_ms * 1000.0 / cnt) if cnt > 0 else 0
    d2c_avg_us = (d2c_ms * 1000.0 / cnt) if cnt > 0 else 0
    c2a_avg_us = (c2a_ms * 1000.0 / c2a_cnt) if c2a_cnt > 0 else 0
    a2u_avg_us = (a2u_ms * 1000.0 / a2u_cnt) if a2u_cnt > 0 else 0

    ebpf_sum_ms = q2d_ms + d2c_ms + c2a_ms + a2u_ms
    ebpf_avg_us = q2d_avg_us + d2c_avg_us + c2a_avg_us + a2u_avg_us

    print(f" [{op_name}] IO Count : {cnt:,} (C2A={c2a_cnt:,}, A2U={a2u_cnt:,})")
    print(f"  - Total Bytes : {bpf_bytes:,} B")
    print(f"  - Bandwidth   : {bpf_bw_mb:>10.2f} MB/s")
    print(
        f"  - QD          : Curr={bpf_stats.get('current_qd', 0)}, Max={bpf_stats.get('max_qd', 0)}"
    )
    print(
        f"  - Full Stack (Run) : Sum = {ebpf_sum_ms:>10.2f} ms | Avg = {ebpf_avg_us:>8.2f} us"
    )

    q2d_hist = bpf_stats.get("q2d_hist")
    d2c_hist = bpf_stats.get("d2c_hist")
    if q2d_hist or d2c_hist:
        q2d_p = compute_percentiles(q2d_hist or [0] * LAT_HIST_BUCKETS)
        d2c_p = compute_percentiles(d2c_hist or [0] * LAT_HIST_BUCKETS)
        print(
            "  - Q2D pct     : "
            f"p50={_fmt_us(q2d_p[50])}  p95={_fmt_us(q2d_p[95])}  "
            f"p99={_fmt_us(q2d_p[99])}  p99.9={_fmt_us(q2d_p[99.9])}"
        )
        print(
            "  - D2C pct     : "
            f"p50={_fmt_us(d2c_p[50])}  p95={_fmt_us(d2c_p[95])}  "
            f"p99={_fmt_us(d2c_p[99])}  p99.9={_fmt_us(d2c_p[99.9])}"
        )

    hist = bpf_stats.get("size_hist", [0, 0, 0, 0])
    if cnt > 0 and sum(hist) > 0:
        labels = ["<= 4KB", "4K-32K", "32K-128K", "> 128KB"]
        print(f"  - IO Size Dist :")
        for i in range(4):
            count = hist[i]
            ratio = (count / cnt) * 100
            bar = "█" * int(ratio / 5)
            print(f"      {labels[i]:>10} : [{bar:<20}] {ratio:>5.1f}% ({count:,})")

    lba_hist = bpf_stats.get("lba_hist", [])
    if cnt > 0 and len(lba_hist) == LBA_BUCKETS and sum(lba_hist) > 0:
        max_val = max(lba_hist)
        spark_chars = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        sparkline = ""
        for val in lba_hist:
            if val == 0:
                sparkline += spark_chars[0]
            else:
                idx = int((val / max_val) * 8)
                if idx == 0:
                    idx = 1
                sparkline += spark_chars[idx]
        print(f"  - LBA Heatmap  : [{sparkline}] (Scale: 0 ~ Max)")
    print()
    return cnt, q2d_ms, d2c_ms, c2a_cnt, c2a_ms


def print_final_summary(raw_json, effective_duration, mode):
    try:
        bpf_data = json.loads(raw_json)
        print("\n" + "=" * 100)
        print(
            f" [ FIO PROFILING FINAL REPORT | Duration: {effective_duration:.2f} seconds ]"
        )
        print("=" * 100)

        sys_stats = bpf_data.get("libaio_overhead", {})
        c2a_read = (
            sys_stats.get("c2a_read_count", 0),
            sys_stats.get("c2a_read_total", 0) / 1000000.0,
        )
        c2a_write = (
            sys_stats.get("c2a_write_count", 0),
            sys_stats.get("c2a_write_total", 0) / 1000000.0,
        )
        c2a_flush = (
            sys_stats.get("c2a_flush_count", 0),
            sys_stats.get("c2a_flush_total", 0) / 1000000.0,
        )
        a2u_read = (
            sys_stats.get("a2u_read_count", 0),
            sys_stats.get("a2u_read_total", 0) / 1000000.0,
        )
        a2u_write = (
            sys_stats.get("a2u_write_count", 0),
            sys_stats.get("a2u_write_total", 0) / 1000000.0,
        )
        a2u_flush = (
            sys_stats.get("a2u_flush_count", 0),
            sys_stats.get("a2u_flush_total", 0) / 1000000.0,
        )

        phase_stats = {"U2Q": {}, "Q2D": {}, "D2C": {}, "C2A": {}, "A2U": {}}
        tot_q2d_cnt = tot_q2d_ms = tot_d2c_ms = 0

        # loop/ram/dm-/md 등 가상 디바이스 제외하고 비율 계산
        monitored_devs = [d for d in bpf_data["devices"]
                          if _is_monitored_dev(get_real_dev_name(d["dev_name"]))]
        total_sys_ios = sum(
            sum(op["total_count"] for op in dev["operations"].values())
            for dev in monitored_devs
        )

        for dev in monitored_devs:
            ops = dev["operations"]
            bpf_total_cnt = sum(op["total_count"] for op in ops.values())
            if bpf_total_cnt > 0 and bpf_total_cnt > (total_sys_ios * 0.05):
                real_name = get_real_dev_name(dev["dev_name"])
                print(f" Target Device: {dev['dev_name']} [{real_name}]")
                sqcq = dev.get("sqcq", {}) or {}
                _same = sqcq.get("same", 0)
                _diff = sqcq.get("diff", 0)
                _tot = _same + _diff
                if _tot > 0:
                    print(f"   SQ↔CQ same={_same:,} ({_same/_tot*100:.1f}%) | diff={_diff:,} ({_diff/_tot*100:.1f}%) "
                          f"→ {'NUMA-local OK' if _diff/_tot < 0.05 else 'CROSS-CPU completion (IRQ affinity 확인)'}")
                print()

                for op_name, bpf_src, c2a_data, a2u_data in [
                    ("READ", ops.get("read", {}), c2a_read, a2u_read),
                    ("WRITE", ops.get("write", {}), c2a_write, a2u_write),
                    ("READ-AHEAD", ops.get("read_ahead", {}), None, None),
                    ("FLUSH", ops.get("flush", {}), c2a_flush, a2u_flush),
                ]:
                    if bpf_src:
                        q_cnt, q_ms, d_ms, c_cnt, c_ms = print_op_stats(
                            op_name, bpf_src, c2a_data, a2u_data, effective_duration
                        )
                        tot_q2d_cnt += q_cnt
                        tot_q2d_ms += q_ms
                        tot_d2c_ms += d_ms
                        phase_stats["Q2D"][op_name] = (
                            q_cnt,
                            q_ms,
                            (q_ms * 1000.0 / q_cnt) if q_cnt > 0 else 0,
                        )
                        phase_stats["D2C"][op_name] = (
                            q_cnt,
                            d_ms,
                            (d_ms * 1000.0 / q_cnt) if q_cnt > 0 else 0,
                        )
                        if c2a_data:
                            phase_stats["C2A"][op_name] = (
                                c_cnt,
                                c_ms,
                                (c_ms * 1000.0 / c_cnt) if c_cnt > 0 else 0,
                            )
                        if a2u_data:
                            a_cnt, a_ms = a2u_data
                            phase_stats["A2U"][op_name] = (
                                a_cnt,
                                a_ms,
                                (a_ms * 1000.0 / a_cnt) if a_cnt > 0 else 0,
                            )

        u2q_cnt = sys_stats.get("u2q_count", 0)
        u2q_sum_ms = sys_stats.get("u2q_lat_total", 0) / 1000000.0
        phase_stats["U2Q"]["Total"] = (
            u2q_cnt,
            u2q_sum_ms,
            (
                (sys_stats.get("u2q_lat_total", 0) / 1000.0 / u2q_cnt)
                if u2q_cnt > 0
                else 0
            ),
        )

        tot_c2a_cnt = c2a_read[0] + c2a_write[0] + c2a_flush[0]
        tot_c2a_ms = c2a_read[1] + c2a_write[1] + c2a_flush[1]
        phase_stats["C2A"]["Total"] = (
            tot_c2a_cnt,
            tot_c2a_ms,
            (tot_c2a_ms * 1000.0 / tot_c2a_cnt) if tot_c2a_cnt > 0 else 0,
        )

        tot_a2u_cnt = a2u_read[0] + a2u_write[0] + a2u_flush[0]
        tot_a2u_ms = a2u_read[1] + a2u_write[1] + a2u_flush[1]
        phase_stats["A2U"]["Total"] = (
            tot_a2u_cnt,
            tot_a2u_ms,
            (tot_a2u_ms * 1000.0 / tot_a2u_cnt) if tot_a2u_cnt > 0 else 0,
        )

        phase_stats["Q2D"]["Total"] = (
            tot_q2d_cnt,
            tot_q2d_ms,
            (tot_q2d_ms * 1000.0 / tot_q2d_cnt) if tot_q2d_cnt > 0 else 0,
        )
        phase_stats["D2C"]["Total"] = (
            tot_q2d_cnt,
            tot_d2c_ms,
            (tot_d2c_ms * 1000.0 / tot_q2d_cnt) if tot_q2d_cnt > 0 else 0,
        )

        table_width = 100
        print("-" * table_width)
        print(f" {'[ FULL STACK LATENCY BREAKDOWN ]':^{table_width - 2}}")
        print("-" * table_width)
        print(
            f" {'Phase':<18} | {'Metric':<10} | {'Total':>12} | {'READ':>12} | {'WRITE':>12} | {'READ-AHEAD':>10} | {'FLUSH':>8}"
        )
        print("-" * table_width)

        def print_phase(phase_name, phase_key):
            stats = phase_stats.get(phase_key, {})

            def get_val(op, idx):
                if op not in stats or stats.get(op, (0, 0, 0))[0] == 0:
                    return "-"
                val = stats[op][idx]
                return f"{int(val):,}" if idx == 0 else f"{val:.2f}"

            tot, r, w, ra, fl = (
                get_val("Total", 0),
                get_val("READ", 0),
                get_val("WRITE", 0),
                get_val("READ-AHEAD", 0),
                get_val("FLUSH", 0),
            )
            if phase_key == "U2Q":
                r = w = ra = fl = "-"
            if phase_key in ["C2A", "A2U"]:
                ra = "-"
            print(
                f" {phase_name:<18} | Call Count | {tot:>12} | {r:>12} | {w:>12} | {ra:>10} | {fl:>8}"
            )

            tot, r, w, ra, fl = (
                get_val("Total", 1),
                get_val("READ", 1),
                get_val("WRITE", 1),
                get_val("READ-AHEAD", 1),
                get_val("FLUSH", 1),
            )
            if phase_key == "U2Q":
                r = w = ra = fl = "-"
            if phase_key in ["C2A", "A2U"]:
                ra = "-"
            print(
                f" {'':<18} | Sum (ms)   | {tot:>12} | {r:>12} | {w:>12} | {ra:>10} | {fl:>8}"
            )

            tot, r, w, ra, fl = (
                get_val("Total", 2),
                get_val("READ", 2),
                get_val("WRITE", 2),
                get_val("READ-AHEAD", 2),
                get_val("FLUSH", 2),
            )
            if phase_key == "U2Q":
                r = w = ra = fl = "-"
            if phase_key in ["C2A", "A2U"]:
                ra = "-"
            print(
                f" {'':<18} | Avg (us)   | {tot:>12} | {r:>12} | {w:>12} | {ra:>10} | {fl:>8}"
            )
            print("-" * table_width)

        print_phase("U2Q (User->BLK_Q)", "U2Q")
        print_phase("Q2D (BLK_Q->Disp)", "Q2D")
        print_phase("D2C (Disp->Compl)", "D2C")
        if mode != "generic":
            print_phase("C2A (Compl->AIO)", "C2A")
            print_phase("A2U (AIO->User)", "A2U")
        print("=" * table_width)
    except Exception as e:
        print(f"[-] Parsing Error in Final Summary: {e}")


def run_workload_thread(cmd, script_file):
    try:
        if cmd:
            subprocess.run(cmd, shell=True)
        elif script_file:
            subprocess.run(f"bash {script_file}", shell=True)
        else:
            while True:
                time.sleep(1)
    except Exception as e:
        print(f"\n[-] Error running workload: {e}")
    finally:
        os.kill(os.getpid(), signal.SIGINT)


def run_benchmark(mode="generic", cmd=None, script_file=None, interval=1):
    # io_trace 바이너리는 ebpf/ 안에 있음. cwd 무관하게 동작하도록 절대 경로 사용.
    io_trace_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "io_trace")
    trace_cmd = ["sudo", io_trace_bin, "-i", str(interval)]
    if mode != "generic":
        trace_cmd.extend(["-m", mode])
        print(f"[*] eBPF Tracer starting in: {mode.upper()} Mode")
    else:
        print("[*] eBPF Tracer starting in: Generic Block Mode")

    trace_proc = subprocess.Popen(trace_cmd, stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)

    subprocess.run(
        "echo 3 | sudo tee /proc/sys/vm/drop_caches",
        shell=True,
        stdout=subprocess.DEVNULL,
    )
    os.kill(trace_proc.pid, signal.SIGUSR1)
    time.sleep(0.1)

    if interval > 0:
        print(
            f"[*] Timeseries logging ENABLED (interval: {interval}s) -> {OUTPUT_DIR}/<device>_*.csv"
        )
    else:
        print(
            "[*] Timeseries logging DISABLED (interval: 0). Collecting only final summary."
        )

    # SystemMonitor: CPU/Mem/IRQ/GPU 통합 메트릭. interval>0일 때만 활성.
    sysmon = None
    if interval > 0 and SystemMonitor is not None:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            sysmon = SystemMonitor(
                output_dir=OUTPUT_DIR,
                session_id=SESSION_ID,
                interval=float(interval),
            )
            sysmon.start()
        except Exception as e:
            print(f"[!] SystemMonitor start 실패: {e}")
            sysmon = None

    print("[*] Executing workload...\n")

    t0 = time.time()
    workload_thread = threading.Thread(
        target=run_workload_thread, args=(cmd, script_file), daemon=True
    )
    workload_thread.start()

    json_buffer = []
    in_json = False
    last_csv_save_time = time.time()
    last_valid_json = "{}"

    try:
        while True:
            line = trace_proc.stdout.readline()
            if not line and trace_proc.poll() is not None:
                break

            if "---JSON_START---" in line:
                json_buffer = []
                in_json = True
            elif "---JSON_END---" in line:
                in_json = False
                raw_json = "\n".join(json_buffer).strip()
                last_valid_json = raw_json

                if interval > 0:
                    parse_and_store_metrics(raw_json)
                    if time.time() - last_csv_save_time >= 5:
                        save_csv_buffers()
                        last_csv_save_time = time.time()
            elif in_json:
                json_buffer.append(line)

    except KeyboardInterrupt:
        print("\n[*] Stopping monitoring gracefully...")

    effective_duration = time.time() - t0
    if effective_duration <= 0:
        effective_duration = 1.0

    if trace_proc.poll() is None:
        os.kill(trace_proc.pid, signal.SIGINT)
        bpf_output, _ = trace_proc.communicate()
        if "---JSON_START---" in bpf_output:
            last_valid_json = (
                bpf_output.split("---JSON_START---")[-1]
                .split("---JSON_END---")[0]
                .strip()
            )

    if sysmon is not None:
        try:
            sysmon.stop()
        except Exception as e:
            print(f"[!] SystemMonitor stop 실패: {e}")

    if interval > 0:
        save_csv_buffers()
        print(
            f"\n -> Timeseries data saved to directory: {os.path.abspath(OUTPUT_DIR)}"
        )

    if last_valid_json != "{}":
        print_final_summary(last_valid_json, effective_duration, mode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Universal eBPF I/O Monitor & Profiler"
    )
    parser.add_argument(
        "-m",
        "--mode",
        type=str,
        default="generic",
        choices=["generic", "libaio", "iouring"],
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        help="Logging interval in seconds (float, 예: 0.5 = 500ms). 0이면 CSV/timeseries 비활성.",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-c",
        "--cmd",
        type=str,
        help="Command string to execute (e.g., -c 'fio --name=test')",
    )
    group.add_argument(
        "-f",
        "--file",
        type=str,
        help="Shell script file to execute (e.g., -f ./fio.sh)",
    )

    args = parser.parse_args()
    run_benchmark(
        mode=args.mode, cmd=args.cmd, script_file=args.file, interval=args.interval
    )
