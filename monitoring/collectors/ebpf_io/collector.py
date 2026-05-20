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

# Allow `python3 -m monitoring.collectors.ebpf_io.collector` standalone invocation.
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

try:
    from monitoring.collectors.system import SystemMonitor
except Exception as _e:
    SystemMonitor = None
    print(f"[!] SystemMonitor import 실패: {_e} — system_metrics 수집은 비활성화")

# Default output dir for standalone runs. Session-managed runs override via --output-dir.
OUTPUT_DIR = os.path.join(_PROJ_ROOT, "results", "ebpf_standalone")
SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

prev_metrics = {}
csv_buffers = {}
prev_libaio = {}  # {key: count or total_ns}, per-interval delta 계산용 (u2q_count/lat, c2a_*, a2u_*)
prev_sqcq = {}    # {dev_name: (same, diff)}, SQ↔CQ 일치 카운터의 인터벌 delta 계산용
prev_hists = {}   # {(dev,op,'q2d'|'d2c'): [32 buckets]} — 인터벌 히스토그램 delta 계산
_ebpf_warmed_up = False  # 첫 인터벌(0~1s)은 버리고 delta baseline만 갱신


# BPF op 이름 → libaio_overhead 필드 prefix 매핑. read_ahead/discard는 libaio 경로가 없어 None.
_LIBAIO_OP_KEY = {
    "read": "read",
    "write": "write",
    "flush": "flush",
}


LAT_HIST_BUCKETS = 32
LBA_BUCKETS = 128  # MUST match src/io_trace.h. 변경 시 BPF 재빌드 필요.


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
        "s2q_avg_us_interval",
        "c2a_avg_us_interval",
        "a2u_avg_us_interval",
        "c2c_avg_us_interval",
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
    global prev_metrics, csv_buffers, prev_libaio, prev_hists, _ebpf_warmed_up
    try:
        bpf_data = json.loads(json_str)
        timestamp = datetime.now().strftime("%H:%M:%S")

        # 엔진 overhead(libaio/io_uring) 누적값을 인터벌 delta로 변환 (avg us).
        # 두 블록의 키는 겹치지 않아(u2q_/c2a_/a2u_ vs s2q_/c2c_) 병합해도 안전.
        sys_st = dict(bpf_data.get("libaio_overhead", {}) or {})
        sys_st.update(bpf_data.get("iouring_overhead", {}) or {})

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

        # u2q(libaio) / s2q(io_uring)는 op 구분 없는 글로벌 값 (모든 행에 동일).
        # count 키는 *_count, total 키는 *_lat_total로 비정규 — 명시적으로 처리.
        def _delta_global_us(cnt_k, tot_k):
            curr_c = sys_st.get(cnt_k, 0)
            curr_t = sys_st.get(tot_k, 0)
            prev_c = prev_libaio.get(cnt_k, 0)
            prev_t = prev_libaio.get(tot_k, 0)
            prev_libaio[cnt_k] = curr_c
            prev_libaio[tot_k] = curr_t
            dc = curr_c - prev_c
            dt = curr_t - prev_t
            return (dt / dc / 1000.0) if dc > 0 else 0.0

        u2q_avg_us_interval = _delta_global_us("u2q_count", "u2q_lat_total")
        s2q_avg_us_interval = _delta_global_us("s2q_count", "s2q_lat_total")

        # op별 c2a/a2u(libaio) + c2c(io_uring) avg us delta 미리 계산해두기.
        # 한 인터벌에서 한 엔진만 값이 있고 나머지는 0.
        op_libaio_avg = {}
        for bpf_op, lib_key in _LIBAIO_OP_KEY.items():
            op_libaio_avg[bpf_op] = {
                "c2a": _delta_avg_us(f"c2a_{lib_key}"),
                "a2u": _delta_avg_us(f"a2u_{lib_key}"),
                "c2c": _delta_avg_us(f"c2c_{lib_key}"),
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
                # I/O가 0건인 인터벌의 latency는 "0us"가 아니라 정의 불가다.
                # percentile(compute_percentiles)이 빈 히스토그램에 None을 주는
                # 것과 맞춰, avg도 None으로 둬 CSV 빈칸 → 차트에서 결손 처리.
                q2d_avg_us = (
                    (delta_q2d / delta_count / 1000.0) if delta_count > 0 else None
                )
                d2c_avg_us = (
                    (delta_d2c / delta_count / 1000.0) if delta_count > 0 else None
                )

                current_qd = stats.get("current_qd", 0)
                max_qd = stats.get("max_qd", 0)

                if delta_count > 0:
                    print(
                        f" [{timestamp}] {real_name:<10} {op:<7} | IOPS: {iops:>6,} | BW: {bw_mb:>8.2f} MB/s | QD: {current_qd:>4} (Max: {max_qd:>4})"
                    )

                size_hist = stats.get("size_hist", [0, 0, 0, 0])
                lba_hist = stats.get("lba_hist", [0] * LBA_BUCKETS)

                op_libaio = op_libaio_avg.get(op, {"c2a": 0.0, "a2u": 0.0, "c2c": 0.0})

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
                    "q2d_avg_us_interval": round(q2d_avg_us, 2) if q2d_avg_us is not None else None,
                    "d2c_avg_us_interval": round(d2c_avg_us, 2) if d2c_avg_us is not None else None,
                    "u2q_avg_us_interval": round(u2q_avg_us_interval, 2),
                    "s2q_avg_us_interval": round(s2q_avg_us_interval, 2),
                    "c2a_avg_us_interval": round(op_libaio["c2a"], 2),
                    "a2u_avg_us_interval": round(op_libaio["a2u"], 2),
                    "c2c_avg_us_interval": round(op_libaio["c2c"], 2),
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

                # 첫 인터벌(0~1s)은 row를 버린다 — prev_*는 아래에서 갱신되므로
                # 두 번째 인터벌부터 깨끗한 1s delta가 기록된다.
                if _ebpf_warmed_up:
                    csv_buffers[real_name].append(row)

                prev_metrics[key] = {
                    "count": curr_count,
                    "bytes": curr_bytes,
                    "q2d_tot": curr_q2d_tot,
                    "d2c_tot": curr_d2c_tot,
                }

        _ebpf_warmed_up = True

    except json.JSONDecodeError:
        pass


def print_op_stats(op_name, bpf_stats, comp_phases, duration):
    """comp_phases: [(label, (count, total_ms)), ...] — 엔진 완료측 페이즈
    (libaio: C2A,A2U / io_uring: C2C). Returns (io_count, q2d_ms, d2c_ms,
    comp_out) with comp_out = [(label, count, ms), ...]."""
    if bpf_stats.get("total_count", 0) == 0:
        return 0, 0, 0, []

    cnt = bpf_stats["total_count"]
    bpf_bytes = bpf_stats.get("total_bytes", 0)
    bpf_bw_mb = (bpf_bytes / (1024.0 * 1024.0)) / duration if duration > 0 else 0

    q2d_ms = bpf_stats.get("q2d", {}).get("total_lat_ns", 0) / 1000000.0
    d2c_ms = bpf_stats.get("d2c", {}).get("total_lat_ns", 0) / 1000000.0
    q2d_avg_us = (q2d_ms * 1000.0 / cnt) if cnt > 0 else 0
    d2c_avg_us = (d2c_ms * 1000.0 / cnt) if cnt > 0 else 0

    comp_out = []
    comp_sum_ms = 0.0
    comp_avg_us = 0.0
    comp_desc = []
    for label, data in comp_phases:
        c_cnt, c_ms = data if data else (0, 0)
        comp_out.append((label, c_cnt, c_ms))
        comp_sum_ms += c_ms
        comp_avg_us += (c_ms * 1000.0 / c_cnt) if c_cnt > 0 else 0
        comp_desc.append(f"{label}={c_cnt:,}")

    ebpf_sum_ms = q2d_ms + d2c_ms + comp_sum_ms
    ebpf_avg_us = q2d_avg_us + d2c_avg_us + comp_avg_us

    desc = (" (" + ", ".join(comp_desc) + ")") if comp_desc else ""
    print(f" [{op_name}] IO Count : {cnt:,}{desc}")
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
    return cnt, q2d_ms, d2c_ms, comp_out


def print_final_summary(raw_json, effective_duration, mode):
    try:
        bpf_data = json.loads(raw_json)
        print("\n" + "=" * 100)
        print(
            f" [ FIO PROFILING FINAL REPORT | Duration: {effective_duration:.2f} seconds ]"
        )
        print("=" * 100)

        # 엔진별 페이즈 구성. block 페이즈(Q2D/D2C)는 공통, 제출/완료측은 엔진별:
        #   libaio  → U2Q (제출) / C2A,A2U (완료)
        #   iouring → S2Q (제출) / C2C (완료) — io_uring은 A2U 대응 없음
        if mode == "iouring":
            ov = bpf_data.get("iouring_overhead", {}) or {}
            submit_phase = "S2Q"
            submit_cnt = ov.get("s2q_count", 0)
            submit_total_ns = ov.get("s2q_lat_total", 0)
            comp_phase_names = ["C2C"]

            def _comp_for(lib_key):
                return [("C2C", (ov.get(f"c2c_{lib_key}_count", 0),
                                 ov.get(f"c2c_{lib_key}_total", 0) / 1000000.0))]
        else:
            ov = bpf_data.get("libaio_overhead", {}) or {}
            submit_phase = "U2Q"
            submit_cnt = ov.get("u2q_count", 0)
            submit_total_ns = ov.get("u2q_lat_total", 0)
            comp_phase_names = ["C2A", "A2U"]

            def _comp_for(lib_key):
                return [("C2A", (ov.get(f"c2a_{lib_key}_count", 0),
                                 ov.get(f"c2a_{lib_key}_total", 0) / 1000000.0)),
                        ("A2U", (ov.get(f"a2u_{lib_key}_count", 0),
                                 ov.get(f"a2u_{lib_key}_total", 0) / 1000000.0))]

        # phase_stats[phase][op] = (count, sum_ms, avg_us)
        phase_stats = {p: {} for p in [submit_phase, "Q2D", "D2C"] + comp_phase_names}
        tot_q2d_cnt = tot_q2d_ms = tot_d2c_ms = 0
        comp_totals = {name: [0, 0.0] for name in comp_phase_names}  # [cnt, ms]

        # loop/ram/dm-/md 등 가상 디바이스 제외하고 비율 계산
        monitored_devs = [d for d in bpf_data["devices"]
                          if _is_monitored_dev(get_real_dev_name(d["dev_name"]))]
        total_sys_ios = sum(
            sum(op["total_count"] for op in dev["operations"].values())
            for dev in monitored_devs
        )

        # 표시명 -> (operations 키, _LIBAIO_OP_KEY lib_key). read_ahead는 완료측 없음.
        op_rows = [("READ", "read", "read"), ("WRITE", "write", "write"),
                   ("READ-AHEAD", "read_ahead", None), ("FLUSH", "flush", "flush")]

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

                for op_disp, op_key, lib_key in op_rows:
                    bpf_src = ops.get(op_key, {})
                    if not bpf_src:
                        continue
                    comp_phases = (_comp_for(lib_key) if lib_key
                                   else [(n, (0, 0)) for n in comp_phase_names])
                    q_cnt, q_ms, d_ms, comp_out = print_op_stats(
                        op_disp, bpf_src, comp_phases, effective_duration
                    )
                    tot_q2d_cnt += q_cnt
                    tot_q2d_ms += q_ms
                    tot_d2c_ms += d_ms
                    phase_stats["Q2D"][op_disp] = (
                        q_cnt, q_ms, (q_ms * 1000.0 / q_cnt) if q_cnt > 0 else 0)
                    phase_stats["D2C"][op_disp] = (
                        q_cnt, d_ms, (d_ms * 1000.0 / q_cnt) if q_cnt > 0 else 0)
                    for label, c_cnt, c_ms in comp_out:
                        phase_stats[label][op_disp] = (
                            c_cnt, c_ms,
                            (c_ms * 1000.0 / c_cnt) if c_cnt > 0 else 0)
                        comp_totals[label][0] += c_cnt
                        comp_totals[label][1] += c_ms

        phase_stats[submit_phase]["Total"] = (
            submit_cnt, submit_total_ns / 1000000.0,
            (submit_total_ns / 1000.0 / submit_cnt) if submit_cnt > 0 else 0)
        phase_stats["Q2D"]["Total"] = (
            tot_q2d_cnt, tot_q2d_ms,
            (tot_q2d_ms * 1000.0 / tot_q2d_cnt) if tot_q2d_cnt > 0 else 0)
        phase_stats["D2C"]["Total"] = (
            tot_q2d_cnt, tot_d2c_ms,
            (tot_d2c_ms * 1000.0 / tot_q2d_cnt) if tot_q2d_cnt > 0 else 0)
        for name in comp_phase_names:
            c_cnt, c_ms = comp_totals[name]
            phase_stats[name]["Total"] = (
                c_cnt, c_ms, (c_ms * 1000.0 / c_cnt) if c_cnt > 0 else 0)

        table_width = 100
        print("-" * table_width)
        print(f" {'[ FULL STACK LATENCY BREAKDOWN ]':^{table_width - 2}}")
        print("-" * table_width)
        print(
            f" {'Phase':<18} | {'Metric':<10} | {'Total':>12} | {'READ':>12} | {'WRITE':>12} | {'READ-AHEAD':>10} | {'FLUSH':>8}"
        )
        print("-" * table_width)

        def print_phase(phase_name, phase_key, is_submit, is_comp):
            stats = phase_stats.get(phase_key, {})

            def get_val(op, idx):
                if op not in stats or stats.get(op, (0, 0, 0))[0] == 0:
                    return "-"
                val = stats[op][idx]
                return f"{int(val):,}" if idx == 0 else f"{val:.2f}"

            for metric_idx, metric_name in (
                (0, "Call Count"), (1, "Sum (ms)  "), (2, "Avg (us)  ")
            ):
                tot = get_val("Total", metric_idx)
                r = get_val("READ", metric_idx)
                w = get_val("WRITE", metric_idx)
                ra = get_val("READ-AHEAD", metric_idx)
                fl = get_val("FLUSH", metric_idx)
                # 제출측(U2Q/S2Q)은 op 구분 없는 글로벌 — Total만 의미 있음.
                if is_submit:
                    r = w = ra = fl = "-"
                # 완료측(C2A/A2U/C2C)은 read-ahead 경로가 없어 컬럼 비움.
                if is_comp:
                    ra = "-"
                head = phase_name if metric_idx == 0 else ""
                print(
                    f" {head:<18} | {metric_name} | {tot:>12} | {r:>12} | {w:>12} | {ra:>10} | {fl:>8}"
                )
            print("-" * table_width)

        _phase_disp = {
            "U2Q": "U2Q (User->BLK_Q)", "S2Q": "S2Q (Submit->BLK_Q)",
            "Q2D": "Q2D (BLK_Q->Disp)", "D2C": "D2C (Disp->Compl)",
            "C2A": "C2A (Compl->AIO)", "A2U": "A2U (AIO->User)",
            "C2C": "C2C (Compl->CQE)",
        }
        print_phase(_phase_disp[submit_phase], submit_phase, True, False)
        print_phase(_phase_disp["Q2D"], "Q2D", False, False)
        print_phase(_phase_disp["D2C"], "D2C", False, False)
        if mode != "generic":
            for name in comp_phase_names:
                print_phase(_phase_disp[name], name, False, True)
        print("=" * table_width)
    except Exception as e:
        print(f"[-] Parsing Error in Final Summary: {e}")


def build_summary(bpf_data, duration, mode):
    """Structured end-of-run summary for charting (ebpf_summary_<sid>.json).

    Same numbers as the printed FINAL REPORT but machine-readable, so the
    report/ layer can draw real charts instead of re-parsing stdout text.
    """
    # libaio/io_uring overhead 병합 — 키가 겹치지 않아 안전. 한 모드만 값이 있다.
    sys_st = dict(bpf_data.get("libaio_overhead", {}) or {})
    sys_st.update(bpf_data.get("iouring_overhead", {}) or {})
    u2q_cnt = sys_st.get("u2q_count", 0)
    u2q_avg_us = round(sys_st.get("u2q_lat_total", 0) / 1000.0 / u2q_cnt, 2) if u2q_cnt else 0.0
    s2q_cnt = sys_st.get("s2q_count", 0)
    s2q_avg_us = round(sys_st.get("s2q_lat_total", 0) / 1000.0 / s2q_cnt, 2) if s2q_cnt else 0.0

    out = {"duration_s": round(duration, 2), "mode": mode,
           "u2q_avg_us": u2q_avg_us, "s2q_avg_us": s2q_avg_us, "devices": {},
           "sqcq_matrix": bpf_data.get("sqcq_matrix", [])}

    for dev in bpf_data.get("devices", []):
        real = get_real_dev_name(dev["dev_name"])
        if not _is_monitored_dev(real):
            continue
        sqcq = dev.get("sqcq", {}) or {}
        dev_out = {"sqcq": {"same": sqcq.get("same", 0), "diff": sqcq.get("diff", 0)},
                   "qd_hist": list(dev.get("qd_hist", [])),
                   "ops": {}}
        for op, st in dev.get("operations", {}).items():
            cnt = st.get("total_count", 0)
            if cnt == 0:
                continue
            q2d_avg = st.get("q2d", {}).get("total_lat_ns", 0) / 1000.0 / cnt
            d2c_avg = st.get("d2c", {}).get("total_lat_ns", 0) / 1000.0 / cnt
            c2a_avg = a2u_avg = c2c_avg = 0.0
            lib = _LIBAIO_OP_KEY.get(op)
            if lib:
                cc = sys_st.get(f"c2a_{lib}_count", 0)
                if cc:
                    c2a_avg = sys_st.get(f"c2a_{lib}_total", 0) / 1000.0 / cc
                ac = sys_st.get(f"a2u_{lib}_count", 0)
                if ac:
                    a2u_avg = sys_st.get(f"a2u_{lib}_total", 0) / 1000.0 / ac
                ccc = sys_st.get(f"c2c_{lib}_count", 0)
                if ccc:
                    c2c_avg = sys_st.get(f"c2c_{lib}_total", 0) / 1000.0 / ccc
            q2d_p = compute_percentiles(st.get("q2d_hist") or [0] * LAT_HIST_BUCKETS)
            d2c_p = compute_percentiles(st.get("d2c_hist") or [0] * LAT_HIST_BUCKETS)
            bytes_ = st.get("total_bytes", 0)

            # D2C 세분화: nvme_complete_rq를 받은 I/O(traced_count)에 대한 평균.
            # nvme(device 왕복) + blkc(block 완료) 비율로 D2C를 쪼갠다.
            ds = st.get("d2c_split", {}) or {}
            tc = ds.get("traced_count", 0)
            if tc > 0:
                nvme_avg = round(ds.get("nvme_total_ns", 0) / 1000.0 / tc, 2)
                blkc_avg = round(ds.get("blkc_total_ns", 0) / 1000.0 / tc, 2)
            else:
                nvme_avg = blkc_avg = 0.0

            dev_out["ops"][op] = {
                "io_count": cnt,
                "total_bytes": bytes_,
                "bandwidth_mb_s": round((bytes_ / 1048576.0) / duration, 2) if duration > 0 else 0.0,
                "max_qd": st.get("max_qd", 0),
                "size_hist": list(st.get("size_hist", [0, 0, 0, 0])),
                # libaio는 u2q/c2a/a2u, io_uring은 s2q/c2c만 채워진다 (나머지 0).
                "phase_avg_us": {
                    "u2q": u2q_avg_us if lib else 0.0,
                    "s2q": s2q_avg_us if lib else 0.0,
                    "q2d": round(q2d_avg, 2),
                    "d2c": round(d2c_avg, 2),
                    "c2a": round(c2a_avg, 2),
                    "c2c": round(c2c_avg, 2),
                    "a2u": round(a2u_avg, 2),
                },
                # D2C 세부 — d2c 구간 내부 비율. traced_frac < 1이면 일부 I/O만 추적됨.
                "d2c_split_us": {"nvme": nvme_avg, "blkc": blkc_avg},
                "d2c_traced_frac": round(tc / cnt, 3) if cnt else 0.0,
                "q2d_pct_us": {str(k): (round(v, 2) if v is not None else None)
                               for k, v in q2d_p.items()},
                "d2c_pct_us": {str(k): (round(v, 2) if v is not None else None)
                               for k, v in d2c_p.items()},
            }
        if dev_out["ops"]:
            out["devices"][real] = dev_out
    return out


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


def run_benchmark(mode="generic", cmd=None, script_file=None, interval=1, enable_sysmon=True):
    # io_trace binary is built into src/ next to collector.py.
    io_trace_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "io_trace")
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
    # Session이 구동할 때는 SystemMonitor를 외부에서 띄우므로 enable_sysmon=False로 중복 방지.
    sysmon = None
    if enable_sysmon and interval > 0 and SystemMonitor is not None:
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
        try:
            summary = build_summary(json.loads(last_valid_json), effective_duration, mode)
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            sp = os.path.join(OUTPUT_DIR, f"ebpf_summary_{SESSION_ID}.json")
            with open(sp, "w") as f:
                json.dump(summary, f, indent=2)
            print(f" -> Structured summary: {sp}")
        except Exception as e:
            print(f"[!] ebpf summary write failed: {e}")


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
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="CSV/SystemMonitor output directory. Session-managed runs inject session_dir; standalone defaults to results/ebpf_standalone/.",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="산출 파일명에 쓰일 session id (기본: 모듈 로드 timestamp). 외부 orchestrator와 ID를 맞추기 위함.",
    )
    parser.add_argument(
        "--no-sysmon",
        action="store_true",
        help="SystemMonitor 비활성 (Session이 별도 인스턴스를 띄우는 경우 중복 방지).",
    )

    args = parser.parse_args()
    # CLI override (module-level globals)
    if args.output_dir:
        OUTPUT_DIR = os.path.abspath(args.output_dir)
    if args.session_id:
        SESSION_ID = args.session_id
    run_benchmark(
        mode=args.mode, cmd=args.cmd, script_file=args.file, interval=args.interval,
        enable_sysmon=not args.no_sysmon,
    )
