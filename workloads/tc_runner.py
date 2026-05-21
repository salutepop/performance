"""Test-case runner — discovers and executes workloads/cases/ inside Sessions.

A test case is a workload recipe. Each case runs inside a monitoring Session
so collectors observe it; the Session then renders reports. This module is the
logic behind `pmon.py monitor --tc`.

Two case formats (workloads/cases/):
  - JSON (tcXX_*.json) : static list of fio workloads.
  - Python (tcXX_*.py) : a `Scenario` class with dynamic control flow.
"""

import functools
import glob
import importlib.util
import json
import os
import sys

from monitoring import Session, SystemDiscovery, resolve_ebpf_mode
from .fio_runner import run_fio_job
from .reporter import ResultReporter

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CASES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")
_CONFIG_PATH = os.path.join(_PROJ_ROOT, "config", "system.json")


def _load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _execute_json_tc(tc_data, disks, numa_node, sys_info, reporter, bound_runner,
                     report_formats, ebpf_mode, ebpf_interval):
    tc_name = tc_data.get("tc_name", "Unknown_TC")
    # A case may pin its own disks (e.g. smoke uses a temp image file).
    tc_disks = tc_data.get("disks") or disks

    # For file-path targets (not /dev/*), pre-create a user-owned empty file so
    # a sudo fio run doesn't leave a root-owned file behind.
    for d in tc_disks:
        if not d.startswith("/dev/"):
            parent = os.path.dirname(d)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if not os.path.exists(d):
                with open(d, "wb") as f:
                    f.truncate(1 * 1024 * 1024 * 1024)  # 1 GiB

    print("\n==================================================")
    print(f"> [JSON scenario] {tc_name}")
    print(f"> desc: {tc_data.get('description', '')}")
    print("==================================================")

    for disk in tc_disks:
        disk_label = disk.split("/")[-1]
        print(f"\n[*] target disk: {disk} ------------------------")

        session_dir = reporter.create_session_dir(tc_name, disk_label)
        reporter.save_json(session_dir, "metadata.json",
                           {"system": sys_info, "tc": tc_data})

        with Session(session_dir, sys_info, ebpf_mode=ebpf_mode,
                     ebpf_interval=ebpf_interval, reports=report_formats):
            for wl in tc_data.get("workloads", []):
                result_data = bound_runner(disk=disk, workload=wl, numa_node=numa_node)
                if result_data:
                    reporter.save_json(session_dir, f"fio_{wl['name']}.json", result_data)
                    reporter.print_summary(wl["name"], result_data)


def _execute_python_tc(tc_file, disks, numa_node, sys_info, reporter, bound_runner,
                       report_formats, ebpf_mode, ebpf_interval):
    module_name = os.path.basename(tc_file)[:-3]
    spec = importlib.util.spec_from_file_location(module_name, tc_file)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"[Error] failed to load {tc_file}: {e}")
        return

    if not hasattr(module, "Scenario"):
        print(f"  -> [Skip] {tc_file} has no 'Scenario' class")
        return

    scenario = module.Scenario()
    print("\n==================================================")
    print(f"> [Python scenario] {getattr(scenario, 'tc_name', module_name)}")
    print(f"> desc: {getattr(scenario, 'description', '')}")
    print("==================================================")

    def _run(session_dir, exec_kwargs, meta_type):
        reporter.save_json(session_dir, "metadata.json",
                           {"system": sys_info, "type": meta_type})
        with Session(session_dir, sys_info, ebpf_mode=ebpf_mode,
                     ebpf_interval=ebpf_interval, reports=report_formats):
            scenario.execute(runner_func=bound_runner, reporter=reporter,
                             session_dir=session_dir, numa_node=numa_node,
                             sys_info=sys_info, **exec_kwargs)

    if getattr(scenario, "run_all_disks", False):
        session_dir = reporter.create_session_dir(
            getattr(scenario, "tc_name", module_name), "multi_disk")
        _run(session_dir, {"disks": disks}, "python_multi_disk_scenario")
    else:
        for disk in disks:
            session_dir = reporter.create_session_dir(
                getattr(scenario, "tc_name", module_name), disk.split("/")[-1])
            _run(session_dir, {"disk": disk}, "python_scenario")


def _discover_cases(tc_filter, all_tcs):
    """Resolve which case files to run. Returns sorted list of absolute paths."""
    tc_files = sorted(glob.glob(os.path.join(_CASES_DIR, "*.json"))
                      + glob.glob(os.path.join(_CASES_DIR, "*.py")))
    if tc_filter and tc_filter != "all":
        matched = [f for f in tc_files if tc_filter.lower() in os.path.basename(f).lower()]
        if not matched:
            print(f"[Error] no case in workloads/cases/ matches '{tc_filter}'")
            return None
        print(f"[*] running cases matching '{tc_filter}'")
        return matched
    if all_tcs or tc_filter == "all":
        print(f"[*] all-TC mode: {len(tc_files)} cases will run sequentially")
        return tc_files
    # default: smoke only
    smoke = [f for f in tc_files if "tc00" in os.path.basename(f).lower()]
    if not smoke:
        print("[Error] default smoke (tc00_*) not found. Pass a --tc name or 'all'.")
        return None
    print("[*] default smoke mode: running tc00 only. Use --tc all for every case.")
    return smoke


def run_test_cases(tc_filter=None, all_tcs=False, quick=False,
                   report_formats="md,json,png,pdf",
                   ebpf_toggle="auto", ebpf_interval=1.0):
    """Discover and run test cases inside monitoring Sessions.

    Returns 0 on success, non-zero on error.
    """
    print("=== SSD Performance — test-case runner ===\n")

    discovery = SystemDiscovery()
    sys_info_discovered = discovery.discover_all()
    discovery.save_to_file()

    config_data = _load_json(_CONFIG_PATH)
    sys_info = config_data.get("system", {})
    sys_info["discovered"] = sys_info_discovered

    if not sys_info.get("target_disks"):
        sys_info["target_disks"] = [d["path"] for d in sys_info_discovered["storage"]]
        print(f"[*] auto-set target disks from discovered NVMe: {sys_info['target_disks']}")

    disks = sys_info.get("target_disks", [])
    numa_node = sys_info.get("numa_node")
    fio_path = sys_info.get("fio_path", "fio")
    if not disks:
        print("[Error] no target disks (check config/system.json or auto-discovery)")
        return 1

    tc_files = _discover_cases(tc_filter, all_tcs)
    if tc_files is None:
        return 1

    reporter = ResultReporter()
    print(f"[*] run output dir: {reporter.run_dir}")

    runtime_val = 1 if quick else None
    bound_runner = functools.partial(run_fio_job, fio_path=fio_path,
                                     runtime_override=runtime_val)
    if quick:
        print("[!] Quick mode: every workload forced to 1s runtime\n")

    resolved_ebpf_mode = resolve_ebpf_mode(ebpf_toggle)
    if resolved_ebpf_mode == "off":
        if ebpf_toggle == "auto":
            print("[*] eBPF tracer skipped (io_trace not built). "
                  "Run `make -C monitoring/collectors/ebpf_io/src` to enable.")
    else:
        print(f"[*] eBPF tracer on (auto-detect, interval={ebpf_interval}s)")

    for tc_file in tc_files:
        ext = os.path.splitext(tc_file)[1].lower()
        if ext == ".json":
            _execute_json_tc(_load_json(tc_file), disks, numa_node, sys_info,
                             reporter, bound_runner, report_formats,
                             resolved_ebpf_mode, ebpf_interval)
        elif ext == ".py":
            _execute_python_tc(tc_file, disks, numa_node, sys_info,
                               reporter, bound_runner, report_formats,
                               resolved_ebpf_mode, ebpf_interval)
    return 0
