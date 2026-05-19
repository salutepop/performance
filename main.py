import json
import os
import sys
import glob
import importlib.util
import functools
import argparse

from core.runner import run_fio_job
from core.reporter import ResultReporter


def load_json(file_path):
    if not os.path.exists(file_path):
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def execute_json_tc(
    tc_file, tc_data, disks, numa_node, sys_info, reporter, bound_runner
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

        for wl in tc_data.get("workloads", []):
            result_data = bound_runner(disk=disk, workload=wl, numa_node=numa_node)
            if result_data:
                filename = f"fio_{wl['name']}.json"
                reporter.save_json(session_dir, filename, result_data)
                reporter.print_summary(wl["name"], result_data)


def execute_python_tc(tc_file, disks, numa_node, sys_info, reporter, bound_runner):
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
            scenario.execute(
                disks=disks, # 단일 disk가 아닌 disks 리스트 전달
                runner_func=bound_runner,
                reporter=reporter,
                session_dir=session_dir,
                numa_node=numa_node,
                sys_info=sys_info,  # [추가] 시스템 정보 전달
            )
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

                # 플러그인에 제어권 넘기기
                scenario.execute(
                    disk=disk,
                    runner_func=bound_runner,
                    reporter=reporter,
                    session_dir=session_dir,
                    numa_node=numa_node,
                    sys_info=sys_info,  # [추가] 시스템 정보 전달
                )
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

    for tc_file in tc_files:
        ext = os.path.splitext(tc_file)[1].lower()

        if ext == ".json":
            tc_data = load_json(tc_file)
            execute_json_tc(
                tc_file, tc_data, disks, numa_node, sys_info, reporter, bound_runner
            )

        elif ext == ".py":
            execute_python_tc(
                tc_file, disks, numa_node, sys_info, reporter, bound_runner
            )


if __name__ == "__main__":
    main()
