"""
세션 산출물을 단일 JSON으로 평탄화. 프로그램(스크립트, 대시보드, 회귀 비교
도구 등)이 소비하기 쉬운 형태.

스키마(stable, 컬럼 추가만 허용):
{
  "session_id": "YYYYMMDD_HHMMSS",
  "generated_at": ISO8601,
  "source_dir": "...",
  "topology": {
    "nodes": ["0", ...],
    "nvme_controllers": ["nvme0", ...],
    "gpus": [{"index": 0, "name": "...", "numa_node": "..."}, ...]
  },
  "devices": {
    "<dev_basename>": {
      "sqcq_diff_ratio": {"avg": float, "max": float},
      "ops": {
        "<op>": {
          "total_io": int, "bw_mb_avg": float, "bw_mb_peak": float,
          "q2d_us_avg": float, "d2c_us_avg": float, "qd_peak": int
        }
      }
    }
  },
  "system": {
    "cpu": {"node<N>": {"user_pct_avg": float, "sys_pct_avg": float,
                        "iowait_pct_avg": float, "iowait_pct_peak": float}},
    "memory": {"mem_dirty_mb_peak": float, "mem_writeback_mb_peak": float,
               "loadavg_1m_peak": float},
    "nvme_irq": {"nvme<X>": {"per_s_avg": float, "per_s_peak": float}},
    "gpu": {"gpu<N>": {"sm_pct_peak": float, "power_w_peak": float,
                       "temp_c_peak": float, "mem_used_mb_peak": float}}
  }
}

CLI:
  python3 -m report.summary [--session-dir DIR] [--session-id SID] [-o out.json]
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

from .html_report import _discover_session, _load_topology, _load_csv
from .md_report import _device_aggregates, _system_aggregates


def _g(d, *keys):
    """안전한 nested get."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def build_summary(session_dir, sid):
    topo = _load_topology(session_dir, sid) or {}
    out = {
        "session_id": sid,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": os.path.abspath(session_dir),
        "topology": {
            "nodes": topo.get("nodes", []),
            "nvme_controllers": topo.get("nvme_controllers", []),
            "gpus": [
                {
                    "index": g.get("index"),
                    "name": g.get("name"),
                    "numa_node": g.get("numa_node"),
                    "pci_bus_id": g.get("pci_bus_id"),
                } for g in (topo.get("gpus") or [])
            ],
        },
        "devices": {},
        "system": {"cpu": {}, "memory": {}, "nvme_irq": {}, "gpu": {}},
    }

    # devices
    device_csvs = sorted(
        p for p in glob.glob(os.path.join(session_dir, f"*_{sid}.csv"))
        if not os.path.basename(p).startswith("system_metrics_")
    )
    for dpath in device_csvs:
        h, r = _load_csv(dpath)
        if not h:
            continue
        base = os.path.basename(dpath)
        dname = re.sub(r"_\d{8}_\d{6}\.csv$", "", base)
        da = _device_aggregates(h, r)
        sqcq = da.get("_sqcq_diff_ratio", {})
        ops = {}
        for op, s in da.items():
            if op.startswith("_"):
                continue
            ops[op] = {
                "total_io": int(_g(s, "iops", "sum") or 0),
                "bw_mb_avg":  _g(s, "bw",  "avg"),
                "bw_mb_peak": _g(s, "bw",  "max"),
                "q2d_us_avg": _g(s, "q2d", "avg"),
                "d2c_us_avg": _g(s, "d2c", "avg"),
                "qd_peak":    int(_g(s, "qd", "max") or 0),
            }
        out["devices"][dname] = {
            "sqcq_diff_ratio": {"avg": sqcq.get("avg"), "max": sqcq.get("max")},
            "ops": ops,
        }

    # system
    sys_path = os.path.join(session_dir, f"system_metrics_{sid}.csv")
    sys_h, sys_r = _load_csv(sys_path)
    if sys_h:
        sa = _system_aggregates(sys_h, sys_r)

        # cpu per-node
        cpu = sa.get("cpu", {})
        nodes_seen = sorted({k.replace("node", "").split("_")[0] for k in cpu})
        for node in nodes_seen:
            out["system"]["cpu"][f"node{node}"] = {
                "user_pct_avg":   _g(cpu, f"node{node}_user_pct",   "avg"),
                "sys_pct_avg":    _g(cpu, f"node{node}_sys_pct",    "avg"),
                "iowait_pct_avg": _g(cpu, f"node{node}_iowait_pct", "avg"),
                "iowait_pct_peak": _g(cpu, f"node{node}_iowait_pct", "max"),
            }

        # memory
        mem = sa.get("mem", {})
        out["system"]["memory"] = {
            "mem_available_mb_min": _g(mem, "mem_available_mb", "min"),
            "mem_dirty_mb_peak":    _g(mem, "mem_dirty_mb",     "max"),
            "mem_writeback_mb_peak": _g(mem, "mem_writeback_mb", "max"),
            "pgpgin_per_s_peak":    _g(mem, "pgpgin_per_s",     "max"),
            "pgpgout_per_s_peak":   _g(mem, "pgpgout_per_s",    "max"),
            "loadavg_1m_peak":      _g(mem, "loadavg_1m",       "max"),
        }

        # NVMe IRQ
        irq = sa.get("nvme_irq", {})
        for k, s in irq.items():
            ctrl = k.replace("_irq_per_s", "")
            out["system"]["nvme_irq"][ctrl] = {
                "per_s_avg": s.get("avg"),
                "per_s_peak": s.get("max"),
            }

        # GPU
        gpu = sa.get("gpu", {})
        gpu_ids = sorted({k.split("_")[0] for k in gpu})
        for g in gpu_ids:
            out["system"]["gpu"][g] = {
                "sm_pct_peak":     _g(gpu, f"{g}_sm_pct",       "max"),
                "power_w_peak":    _g(gpu, f"{g}_pwr_w",        "max"),
                "temp_c_peak":     _g(gpu, f"{g}_temp_c",       "max"),
                "mem_used_mb_peak": _g(gpu, f"{g}_mem_used_mb", "max"),
            }

    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="세션 산출물을 단일 JSON 요약으로 평탄화")
    p.add_argument("--session-dir", default="ebpf/csv_results")
    p.add_argument("--session-id", default=None)
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    sd = args.session_dir
    if not os.path.isdir(sd):
        print(f"[!] 세션 디렉터리 없음: {sd}", file=sys.stderr); return 2
    sid = args.session_id or _discover_session(sd)
    if not sid:
        print(f"[!] topology_*.json 없음 in {sd}", file=sys.stderr); return 2

    out_path = args.output or os.path.join(sd, f"summary_{sid}.json")
    data = build_summary(sd, sid)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    print(f"[summary] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
