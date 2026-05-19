import json
import os
import sys
import glob
import signal
import shutil
import subprocess
import time
import importlib.util
import functools
import argparse

from core.runner import run_fio_job
from core.reporter import ResultReporter
from core.monitor import SystemMonitor


_PROJ_ROOT = os.path.dirname(os.path.abspath(__file__))
_IO_TRACE_BIN = os.path.join(_PROJ_ROOT, "ebpf", "io_trace")
_IO_PROFILER_PY = os.path.join(_PROJ_ROOT, "ebpf", "io_profiler.py")


def load_json(file_path):
    if not os.path.exists(file_path):
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _start_monitor(session_dir, sys_info):
    """session_dir에 SystemMonitor를 띄움. session_id는 디렉터리 basename."""
    sid = os.path.basename(session_dir)
    try:
        mon = SystemMonitor(session_dir, session_id=sid, interval=1.0, sys_info=sys_info)
        mon.start()
        return mon, sid
    except Exception as e:
        print(f"  [!] SystemMonitor start failed (reports will use fio JSON only): {e}")
        return None, sid


def _stop_monitor(mon):
    if not mon:
        return
    try:
        mon.stop()
    except Exception as e:
        print(f"  [!] SystemMonitor stop error: {e}")


def _ebpf_available():
    """io_trace 바이너리 + io_profiler.py가 둘 다 있으면 True."""
    return os.path.isfile(_IO_TRACE_BIN) and os.access(_IO_TRACE_BIN, os.X_OK) \
        and os.path.isfile(_IO_PROFILER_PY)


def _start_ebpf(session_dir, sid, mode, interval):
    """io_profiler.py를 subprocess로 띄워 eBPF tracer 가동. 워크로드는 main.py가 별도로 돌림."""
    cmd = [
        sys.executable, _IO_PROFILER_PY,
        "-m", mode,
        "-i", str(interval),
        "--output-dir", session_dir,
        "--session-id", sid,
        "--no-sysmon",  # SystemMonitor는 main.py가 띄움 (중복 방지)
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as e:
        print(f"  [!] eBPF tracer start failed: {e}")
        return None
    # wait for io_trace attach to settle (io_profiler sleeps 1.5s then SIGUSR1 reset)
    time.sleep(2.5)
    if proc.poll() is not None:
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        print(f"  [!] eBPF tracer exited immediately (rc={proc.returncode}): {err.strip()[:300]}")
        return None
    print(f"  [eBPF] tracer started (mode={mode}, interval={interval}s) -> {session_dir}")
    return proc


def _stop_ebpf(proc):
    if not proc:
        return
    try:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print("  [!] eBPF tracer not responding - sending SIGTERM")
            proc.terminate()
            proc.wait(timeout=5)
        print(f"  [eBPF] tracer stopped (rc={proc.returncode})")
    except Exception as e:
        print(f"  [!] eBPF tracer stop error: {e}")


def _run_reports(session_dir, sid, formats):
    """report.__main__.main()을 호출해 html/md/json/png 일괄 생성."""
    if not formats or formats == "none":
        return
    try:
        from report.__main__ import main as report_main
    except ImportError as e:
        print(f"  [!] report module import failed: {e}")
        return
    rc = report_main([
        "--session-dir", session_dir,
        "--session-id", sid,
        "--format", formats,
    ])
    if rc:
        print(f"  [!] some reports failed (rc={rc}) - {session_dir}")
    else:
        print(f"  [*] reports written -> {session_dir}")


def execute_json_tc(
    tc_file, tc_data, disks, numa_node, sys_info, reporter, bound_runner,
    report_formats="none", ebpf_mode="off", ebpf_interval=1.0,
):
    tc_name = tc_data.get("tc_name", "Unknown_TC")

    # JSON에 disks가 명시되어 있으면 그것을 우선 사용 (smoke 등 자체 임시 파일 시나리오)
    tc_disks = tc_data.get("disks") or disks

    # 파일 경로 디스크(/dev/* 아님)면 parent dir + 빈 파일 사전 생성
    # (fio가 sudo로 돌면 root 소유로 만들어지므로 user 소유 빈 파일을 미리 둬서 ownership 유지)
    for d in tc_disks:
        if not d.startswith("/dev/"):
            parent = os.path.dirname(d)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if not os.path.exists(d):
                with open(d, "wb") as f:
                    f.truncate(1 * 1024 * 1024 * 1024)  # 1 GiB

    print(f"\n==================================================")
    print(f"> [JSON scenario] {tc_name}")
    print(f"> desc: {tc_data.get('description', '')}")
    print(f"==================================================")

    for disk in tc_disks:
        disk_label = disk.split("/")[-1]
        print(f"\n[*] target disk: {disk} ------------------------")

        session_dir = reporter.create_session_dir(tc_name, disk_label)
        reporter.save_json(
            session_dir, "metadata.json", {"system": sys_info, "tc": tc_data}
        )

        mon, sid = _start_monitor(session_dir, sys_info)
        ebpf_proc = _start_ebpf(session_dir, sid, ebpf_mode, ebpf_interval) if ebpf_mode != "off" else None
        try:
            for wl in tc_data.get("workloads", []):
                result_data = bound_runner(disk=disk, workload=wl, numa_node=numa_node)
                if result_data:
                    filename = f"fio_{wl['name']}.json"
                    reporter.save_json(session_dir, filename, result_data)
                    reporter.print_summary(wl["name"], result_data)
        finally:
            _stop_ebpf(ebpf_proc)
            _stop_monitor(mon)
        _run_reports(session_dir, sid, report_formats)


def execute_python_tc(tc_file, disks, numa_node, sys_info, reporter, bound_runner,
                      report_formats="none", ebpf_mode="off", ebpf_interval=1.0):
    # 1. 누락되었던 모듈 동적 로드 부분 (완성)
    module_name = os.path.basename(tc_file)[:-3]
    spec = importlib.util.spec_from_file_location(module_name, tc_file)
    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"[Error] failed to load {tc_file}: {e}")
        return

    # 2. 시나리오 실행
    if hasattr(module, "Scenario"):
        scenario = module.Scenario()

        print(f"\n==================================================")
        print(f"> [Python scenario] {getattr(scenario, 'tc_name', module_name)}")
        print(f"> desc: {getattr(scenario, 'description', '')}")
        print(f"==================================================")

        # [수정] 시나리오가 모든 디스크를 한꺼번에 제어하고 싶어하는 경우 (예: Scalability 측정)
        if getattr(scenario, "run_all_disks", False):
            session_dir = reporter.create_session_dir(
                getattr(scenario, "tc_name", module_name), "multi_disk"
            )
            reporter.save_json(
                session_dir,
                "metadata.json",
                {"system": sys_info, "type": "python_multi_disk_scenario"},
            )
            mon, sid = _start_monitor(session_dir, sys_info)
            ebpf_proc = _start_ebpf(session_dir, sid, ebpf_mode, ebpf_interval) if ebpf_mode != "off" else None
            try:
                scenario.execute(
                    disks=disks, # 단일 disk가 아닌 disks 리스트 전달
                    runner_func=bound_runner,
                    reporter=reporter,
                    session_dir=session_dir,
                    numa_node=numa_node,
                    sys_info=sys_info,  # [추가] 시스템 정보 전달
                )
            finally:
                _stop_ebpf(ebpf_proc)
                _stop_monitor(mon)
            _run_reports(session_dir, sid, report_formats)
        else:
            for disk in disks:
                disk_label = disk.split("/")[-1]
                session_dir = reporter.create_session_dir(
                    getattr(scenario, "tc_name", module_name), disk_label
                )
                reporter.save_json(
                    session_dir,
                    "metadata.json",
                    {"system": sys_info, "type": "python_scenario"},
                )

                mon, sid = _start_monitor(session_dir, sys_info)
                ebpf_proc = _start_ebpf(session_dir, sid, ebpf_mode, ebpf_interval) if ebpf_mode != "off" else None
                try:
                    # 플러그인에 제어권 넘기기
                    scenario.execute(
                        disk=disk,
                        runner_func=bound_runner,
                        reporter=reporter,
                        session_dir=session_dir,
                        numa_node=numa_node,
                        sys_info=sys_info,  # [추가] 시스템 정보 전달
                    )
                finally:
                    _stop_ebpf(ebpf_proc)
                    _stop_monitor(mon)
                _run_reports(session_dir, sid, report_formats)
    else:
        print(f"  -> [Skip] {tc_file} has no 'Scenario' class")


def main():
    # [추가] argparse 셋업
    parser = argparse.ArgumentParser(
        description="Advanced SSD Performance Evaluation Framework"
    )
    parser.add_argument(
        "-t",
        "--tc",
        type=str,
        help="Run a specific test case by name or substring (e.g. tc03)",
    )
    parser.add_argument(
        "-q",
        "--quick",
        action="store_true",
        help="Quick mode: every workload runs ~1s (for fast sanity checks)",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Run every test case (default runs tc00_smoke only)",
    )
    parser.add_argument(
        "--report",
        default="html,md,json,png,pdf",
        help=(
            "Comma-separated report formats (html,md,json,png,pdf) or 'none'. "
            "Default: 'html,md,json,png,pdf' - SystemMonitor collects topology/CSV per session "
            "and report.* modules emit each format. PDF uses matplotlib only (works offline/CLI)."
        ),
    )
    parser.add_argument(
        "--ebpf",
        choices=["auto", "on", "off"],
        default="auto",
        help=(
            "eBPF I/O tracer toggle (default auto: enable if ebpf/io_trace binary exists, "
            "skip otherwise). 'on' forces enable, 'off' forces disable."
        ),
    )
    parser.add_argument(
        "--ebpf-mode",
        choices=["generic", "libaio", "iouring"],
        default="libaio",
        help="eBPF tracer mode (default libaio: also measures U2Q/C2A/A2U phases).",
    )
    parser.add_argument(
        "--ebpf-interval",
        type=float,
        default=1.0,
        help="eBPF CSV polling interval in seconds (default 1.0). 0 disables timeseries (final summary only).",
    )
    args = parser.parse_args()

    from core.discovery import SystemDiscovery

    print("=== Advanced SSD Performance Evaluation Framework ===\n")

    # [추가] 시스템 정보 자동 탐색
    discovery = SystemDiscovery()
    sys_info_discovered = discovery.discover_all()
    discovery.save_to_file()

    # 기존 설정 로드
    config_data = load_json("config/system.json")
    sys_info = config_data.get("system", {})
    
    # [추가] 발견된 정보를 기존 sys_info에 병합
    sys_info["discovered"] = sys_info_discovered
    
    # 만약 config에 target_disks가 비어있다면 발견된 NVMe 장치로 자동 설정
    if not sys_info.get("target_disks"):
        sys_info["target_disks"] = [d["path"] for d in sys_info_discovered["storage"]]
        print(f"[*] auto-set target disks from discovered NVMe: {sys_info['target_disks']}")

    disks = sys_info.get("target_disks", [])
    numa_node = sys_info.get("numa_node")
    fio_path = sys_info.get("fio_path", "fio")

    if not disks:
        print("[Error] no target disks (check config/system.json or auto-discovery)")
        sys.exit(1)

    reporter = ResultReporter()
    print(f"[*] run output dir: {reporter.run_dir}")
    tc_files = sorted(glob.glob("test_cases/*.json") + glob.glob("test_cases/*.py"))

    # [추가] 터미널에서 --tc 옵션을 주었다면, 해당 키워드가 포함된 파일만 필터링
    if args.tc:
        filtered_files = [f for f in tc_files if args.tc.lower() in f.lower()]
        if not filtered_files:
            print(f"[Error] no test_cases/ file matches '{args.tc}'")
            sys.exit(1)
        tc_files = filtered_files
        print(f"[*] running only scenarios matching '{args.tc}'")
    elif not args.all:
        # 기본 동작: tc00_smoke만 실행. -a/--all 또는 -t로 명시할 때만 전체/지정 TC 실행
        smoke_files = [f for f in tc_files if "tc00" in os.path.basename(f).lower()]
        if not smoke_files:
            print("[Error] default smoke (tc00_*) not found. Use -a for all TCs or -t to pick one.")
            sys.exit(1)
        tc_files = smoke_files
        print("[*] default smoke mode: running tc00_smoke only. Use -a/--all for every TC.")
    else:
        print(f"[*] all-TC mode: {len(tc_files)} scenarios will run sequentially")

    # [수정] Quick 모드인 경우 런타임을 1초로 고정하는 오버라이드 설정
    runtime_val = 1 if args.quick else None
    bound_runner = functools.partial(run_fio_job, fio_path=fio_path, runtime_override=runtime_val)
    
    if args.quick:
        print("[!] Quick mode: every workload forced to 1s runtime\n")

    # eBPF 활성 여부 resolve
    if args.ebpf == "off":
        resolved_ebpf_mode = "off"
    elif args.ebpf == "on":
        if not _ebpf_available():
            print(f"[Error] --ebpf on but ebpf/io_trace binary missing or not executable. Run `cd ebpf && make` first.")
            sys.exit(1)
        resolved_ebpf_mode = args.ebpf_mode
    else:  # auto
        if _ebpf_available():
            resolved_ebpf_mode = args.ebpf_mode
            print(f"[*] eBPF tracer auto-on (mode={resolved_ebpf_mode}, interval={args.ebpf_interval}s)")
        else:
            resolved_ebpf_mode = "off"
            print("[*] eBPF tracer skipped (ebpf/io_trace not built). Run `cd ebpf && make` to enable.")

    for tc_file in tc_files:
        ext = os.path.splitext(tc_file)[1].lower()

        if ext == ".json":
            tc_data = load_json(tc_file)
            execute_json_tc(
                tc_file, tc_data, disks, numa_node, sys_info, reporter, bound_runner,
                report_formats=args.report,
                ebpf_mode=resolved_ebpf_mode, ebpf_interval=args.ebpf_interval,
            )

        elif ext == ".py":
            execute_python_tc(
                tc_file, disks, numa_node, sys_info, reporter, bound_runner,
                report_formats=args.report,
                ebpf_mode=resolved_ebpf_mode, ebpf_interval=args.ebpf_interval,
            )


if __name__ == "__main__":
    main()
