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
        print(f"  [!] SystemMonitor 시작 실패 (리포트는 fio JSON 기반으로만 생성): {e}")
        return None, sid


def _stop_monitor(mon):
    if not mon:
        return
    try:
        mon.stop()
    except Exception as e:
        print(f"  [!] SystemMonitor 정지 중 오류: {e}")


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
        print(f"  [!] eBPF tracer 시작 실패: {e}")
        return None
    # io_trace attach 안정화 대기 (io_profiler 내부는 1.5s sleep 후 SIGUSR1 reset)
    time.sleep(2.5)
    if proc.poll() is not None:
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        print(f"  [!] eBPF tracer가 즉시 종료됨 (rc={proc.returncode}): {err.strip()[:300]}")
        return None
    print(f"  [eBPF] tracer started (mode={mode}, interval={interval}s) → {session_dir}")
    return proc


def _stop_ebpf(proc):
    if not proc:
        return
    try:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print("  [!] eBPF tracer 응답 없음 — SIGTERM")
            proc.terminate()
            proc.wait(timeout=5)
        print(f"  [eBPF] tracer stopped (rc={proc.returncode})")
    except Exception as e:
        print(f"  [!] eBPF tracer 정지 중 오류: {e}")


def _run_reports(session_dir, sid, formats):
    """report.__main__.main()을 호출해 html/md/json/png 일괄 생성."""
    if not formats or formats == "none":
        return
    try:
        from report.__main__ import main as report_main
    except ImportError as e:
        print(f"  [!] 리포트 모듈 import 실패: {e}")
        return
    rc = report_main([
        "--session-dir", session_dir,
        "--session-id", sid,
        "--format", formats,
    ])
    if rc:
        print(f"  [!] 일부 리포트 생성 실패 (rc={rc}) — {session_dir}")
    else:
        print(f"  [*] 리포트 생성 완료 → {session_dir}")


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
    print(f"▶ [JSON 시나리오] {tc_name}")
    print(f"▶ 설명: {tc_data.get('description', '')}")
    print(f"==================================================")

    for disk in tc_disks:
        disk_label = disk.split("/")[-1]
        print(f"\n[*] 타겟 디스크: {disk} ------------------------")

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
        print(f"[Error] {tc_file} 로드 중 오류 발생: {e}")
        return

    # 2. 시나리오 실행
    if hasattr(module, "Scenario"):
        scenario = module.Scenario()

        print(f"\n==================================================")
        print(f"▶ [Python 시나리오] {getattr(scenario, 'tc_name', module_name)}")
        print(f"▶ 설명: {getattr(scenario, 'description', '')}")
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
        print(f"  -> [Skip] {tc_file} 내부에 'Scenario' 클래스가 없습니다.")


def main():
    # [추가] argparse 셋업
    parser = argparse.ArgumentParser(
        description="Advanced SSD Performance Evaluation Framework"
    )
    parser.add_argument(
        "-t",
        "--tc",
        type=str,
        help="실행할 특정 테스트 케이스의 이름이나 키워드 (예: tc03)",
    )
    parser.add_argument(
        "-q",
        "--quick",
        action="store_true",
        help="빠른 검증 모드 (모든 테스트를 1초 내외로 실행)",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="모든 테스트 케이스를 실행 (기본값은 tc00_smoke만 실행)",
    )
    parser.add_argument(
        "--report",
        default="html,md,json,png",
        help=(
            "자동 생성할 리포트 포맷 콤마 구분 (html,md,json,png 또는 'none'). "
            "기본: 'html,md,json,png' — SystemMonitor가 세션별로 topology/CSV를 "
            "수집하고 종료 후 report.* 모듈로 일괄 생성."
        ),
    )
    parser.add_argument(
        "--ebpf",
        choices=["auto", "on", "off"],
        default="auto",
        help=(
            "eBPF I/O tracer 가동 여부 (기본 auto: ebpf/io_trace 바이너리 존재 시 자동 on, "
            "없으면 skip). on은 강제, off는 강제 비활성."
        ),
    )
    parser.add_argument(
        "--ebpf-mode",
        choices=["generic", "libaio", "iouring"],
        default="libaio",
        help="eBPF tracer 모드 (기본 libaio: U2Q/C2A/A2U 페이즈까지 측정).",
    )
    parser.add_argument(
        "--ebpf-interval",
        type=float,
        default=1.0,
        help="eBPF CSV 폴링 간격 초 (기본 1.0). 0이면 timeseries 비활성, 최종 summary만.",
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
        print(f"[*] 발견된 NVMe 장치를 테스트 대상으로 자동 설정합니다: {sys_info['target_disks']}")

    disks = sys_info.get("target_disks", [])
    numa_node = sys_info.get("numa_node")
    fio_path = sys_info.get("fio_path", "fio")

    if not disks:
        print("[Error] 테스트할 장치를 찾을 수 없습니다. (config/system.json 또는 자동 탐색 실패)")
        sys.exit(1)

    reporter = ResultReporter()
    print(f"[*] 이번 평가 결과 폴더: {reporter.run_dir}")
    tc_files = sorted(glob.glob("test_cases/*.json") + glob.glob("test_cases/*.py"))

    # [추가] 터미널에서 --tc 옵션을 주었다면, 해당 키워드가 포함된 파일만 필터링
    if args.tc:
        filtered_files = [f for f in tc_files if args.tc.lower() in f.lower()]
        if not filtered_files:
            print(f"[Error] test_cases/ 폴더에 '{args.tc}'가 포함된 파일이 없습니다.")
            sys.exit(1)
        tc_files = filtered_files
        print(f"[*] 타겟 실행 모드: '{args.tc}' 키워드가 포함된 시나리오만 실행합니다.")
    elif not args.all:
        # 기본 동작: tc00_smoke만 실행. -a/--all 또는 -t로 명시할 때만 전체/지정 TC 실행
        smoke_files = [f for f in tc_files if "tc00" in os.path.basename(f).lower()]
        if not smoke_files:
            print("[Error] 기본 스모크 테스트(tc00_*)를 찾을 수 없습니다. -a 옵션으로 전체 실행하거나 -t로 TC를 지정하세요.")
            sys.exit(1)
        tc_files = smoke_files
        print("[*] 기본 스모크 모드: tc00_smoke만 실행합니다. 전체 TC 실행은 -a/--all 옵션을 사용하세요.")
    else:
        print(f"[*] 전체 TC 실행 모드: {len(tc_files)}개의 시나리오를 순차 실행합니다.")

    # [수정] Quick 모드인 경우 런타임을 1초로 고정하는 오버라이드 설정
    runtime_val = 1 if args.quick else None
    bound_runner = functools.partial(run_fio_job, fio_path=fio_path, runtime_override=runtime_val)
    
    if args.quick:
        print("[!] Quick 모드가 활성화되었습니다. 모든 테스트는 1초 동안만 수행됩니다.\n")

    # eBPF 활성 여부 resolve
    if args.ebpf == "off":
        resolved_ebpf_mode = "off"
    elif args.ebpf == "on":
        if not _ebpf_available():
            print(f"[Error] --ebpf on 인데 ebpf/io_trace 바이너리가 없거나 실행 불가. cd ebpf && make 후 재시도.")
            sys.exit(1)
        resolved_ebpf_mode = args.ebpf_mode
    else:  # auto
        if _ebpf_available():
            resolved_ebpf_mode = args.ebpf_mode
            print(f"[*] eBPF tracer 자동 활성 (mode={resolved_ebpf_mode}, interval={args.ebpf_interval}s)")
        else:
            resolved_ebpf_mode = "off"
            print("[*] eBPF tracer skip (ebpf/io_trace 미빌드). 활성하려면 cd ebpf && make")

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
