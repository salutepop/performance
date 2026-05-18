# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

SSD 성능 측정 및 분석 자동화 프레임워크. `fio`를 엔진으로 쓰고, 그 위에서 다양한 시나리오(코어 순회, NUMA distance, multi-disk scalability, 3D 파라미터 sweep 등)를 Python/JSON으로 정의한다. 결과는 세션 단위 디렉터리로 떨어지고, eBPF 기반 보조 트레이서로 블록 계층 I/O를 따로 수집할 수도 있다.

설명/리포트/주석은 한국어가 기본. 새 코드를 추가할 때도 사용자 출력은 한국어로 유지.

## Common commands

```bash
# 전체 TC 실행 (test_cases/ 하위 .json + .py 전부)
python3 main.py

# 특정 TC만 실행 (파일명 부분 매칭, case-insensitive)
python3 main.py -t tc03

# Quick 검증 모드: runtime을 1초로 강제, ramp_time=0
python3 main.py -q
python3 main.py -t tc06 -q
```

NVMe raw 디바이스에 직접 쓰는 워크로드가 많아 root/sudo 권한이 거의 항상 필요하다. `tc02_dirty_gc.py` 같은 케이스는 `/proc/sys/vm/drop_caches`에 쓴다.

### eBPF I/O 트레이서 (ebpf/)

> eBPF 서브시스템은 별도 문서가 있다: [`ebpf/CLAUDE.md`](./ebpf/CLAUDE.md). 이 폴더 코드를 만질 때는 그 문서를 먼저 읽을 것.

### 통합 CLI: `pmon.py`

프로젝트 루트의 `pmon.py`가 모든 흐름을 묶는 진입점.

```bash
./pmon.py run --fio "fio ..."           # fio + eBPF 측정 + 자동 HTML/MD/JSON 리포트
./pmon.py run --script ebpf/fio.sh -m libaio -i 1
./pmon.py report                        # 가장 최근 세션에서 리포트만 (--session-id로 명시 가능)
./pmon.py diff --baseline SID --candidate SID
./pmon.py summary                       # 평탄화 JSON export
```

`run`은 종료 후 `--report` 옵션(`html`/`md`/`json` 콤마구분 or `all`/`none`)에 따라 자동 리포트 생성. 개별 `report.*` 모듈은 그대로 `python3 -m report.X` 로 단독 호출도 가능.

### 리포트 생성 (report/)

세션 산출물(topology_*.json, system_metrics_*.csv, <device>_*.csv)을 자기완결 리포트로 변환. session-id 생략 시 가장 최근 topology_*.json 자동 선택.

- HTML: `python3 -m report.html_report --session-dir <dir> [--session-id SID] [-o out.html]` — topology 요약 + CSV 테이블 + 디바이스별 시계열 차트 3개 (IOPS/BW/D2C latency, operation 색 분리). 차트는 `<script src="https://cdn.jsdelivr.net/npm/chart.js@4">`를 통한 CDN 로드 — 오프라인 환경에서는 차트 자리에 "Chart.js CDN unreachable" 메시지 표시되고 테이블은 정상 렌더.
- Markdown: `python3 -m report.md_report ...` — Top findings(SQ/CQ, iowait, GPU 활동, top NUMA node) + Topology/Device aggregate/System aggregate 요약 테이블만 (포터블 텍스트, ~1.5KB).
- Session diff: `python3 -m report.diff --baseline SID --candidate SID [--session-dir DIR]` — 두 세션 aggregate 비교, op별 IOPS/BW/D2C/peak QD + system CPU/IRQ/GPU 변동률 표. 변동 ≥5%는 ⚠, ≥20%는 ⛔로 표시. SID 대신 절대 경로 폴더도 받음.
- JSON summary: `python3 -m report.summary ...` — 세션 산출물을 단일 평탄화 JSON으로 export (`summary_{sid}.json`). 프로그램적 소비용 (대시보드 입력, 회귀 자동화 등). 스키마는 stable — 컬럼 추가만 허용.

```bash
cd ebpf
make            # io_trace 바이너리 빌드 (clang + bpftool + libbpf 필요)
make run        # sudo ./io_trace
make clean

# Python 래퍼: fio를 돌리면서 eBPF로 block-layer I/O를 캡처
sudo python3 io_profiler.py -m generic -i 1 -c "fio --name=test --filename=/dev/nvme0n1 ..."
sudo python3 io_profiler.py -f ./fio.sh -i 0   # CSV 저장 비활성화
# -m: generic | libaio | iouring
# -i: timeseries CSV 저장 간격(초). 0이면 최종 summary만.
```

빌드 의존성: `clang`, `bpftool`, `libbpf-dev`, `libelf-dev`, `zlib1g-dev`. `vmlinux.h`는 `/sys/kernel/btf/vmlinux`에서 자동 생성된다.

## Architecture

### Execution pipeline (main.py)

1. `core.discovery.SystemDiscovery`가 CPU/NUMA/NVMe/메모리 정보를 자동 탐색해 `config/discovered_system.json`에 떨어뜨린다.
2. `config/system.json`을 로드하고, `target_disks`가 비어있으면 발견된 NVMe namespace로 자동 채운다. 발견된 정보는 `sys_info["discovered"]`에 병합되어 모든 시나리오로 전달된다.
3. `test_cases/*.json` + `test_cases/*.py`를 정렬해서 순회.
4. `functools.partial`로 `run_fio_job`에 `fio_path`와 `runtime_override`를 미리 묶어 `bound_runner`를 만들고, 이걸 시나리오에 주입한다. 시나리오는 fio 경로/quick 모드를 신경 쓸 필요가 없다.
5. `.json` TC는 `execute_json_tc`가 워크로드 리스트를 그대로 순회, `.py` TC는 `execute_python_tc`가 `importlib`로 동적 로드한 뒤 `Scenario` 클래스를 인스턴스화해서 `execute(...)`를 호출한다.

### Two TC formats (test_cases/)

- **JSON (`tcXX_*.json`)**: 정적인 fio 워크로드 목록. `tc_name`, `description`, `workloads[]`만 있으면 된다. `workloads`의 각 항목이 그대로 `run_fio_job`에 들어간다.
- **Python (`tcXX_*.py`)**: `class Scenario` 를 export해야 한다. 동적 제어(전처리 → 측정 → 조건부 분기, 코어 순회, 결과 누적 테이블 출력 등)가 필요할 때 사용. 다음 시그니처 중 하나를 구현한다:
  - 단일 디스크 모드 (기본): `execute(self, disk, runner_func, reporter, session_dir, numa_node, sys_info=None)` — 프레임워크가 `target_disks`를 하나씩 돌면서 호출.
  - Multi-disk 모드: `self.run_all_disks = True`로 표시하면 프레임워크가 디스크 리스트 전체를 한 번에 넘긴다 → `execute(self, disks, runner_func, reporter, session_dir, numa_node, sys_info=None)`.

`sys_info` 인자는 선택적이지만 NUMA/CPU 토폴로지 기반 최적화(TC09, TC10, TC08)에 필수다. 새 TC가 시스템 정보를 쓸 거면 시그니처에 `sys_info=None`을 받도록 해야 한다 — 일부 기존 TC는 아직 안 받고 있어서, 받지 않는 형식과 받는 형식이 섞여 있다.

### core/ modules

- `runner.py::run_fio_job(disk, workload, numa_node, fio_path, runtime_override)` — fio 명령을 빌드해 `subprocess.run`으로 실행하고 JSON을 파싱해 dict로 반환. `disk`가 리스트면 `:`로 join해 multi-target으로 넘긴다. `--direct=1`, `--ioengine=libaio`, `--time_based`, `--group_reporting=1`은 강제. `ramp_time`은 quick 모드에서만 0, 그 외엔 3초 고정. workload dict의 `cpus_allowed`, `size`는 있을 때만 추가된다.
- `reporter.py::ResultReporter` — `results/{timestamp}_{tc_name}_{disk_label}/` 세션 디렉터리 생성, JSON 저장, BW/IOPS/Latency one-line summary 출력. 시나리오가 `reporter.save_json(session_dir, "fio_X.json", result)` 패턴으로 호출한다.
- `discovery.py::SystemDiscovery` — `lscpu`, `/sys/devices/system/node/`, `/sys/class/nvme/`, `/proc/meminfo`, `dmidecode`(권한 있으면), `nvidia-smi`에서 정보 수집. 산출 키: `cpu`, `numa`, `storage`(namespace 단위), `nvme_ctrls`(컨트롤러 단위 정적 attr: model/state/firmware_rev/serial/transport/address/cntrltype/queue_count/numa_node/subsysnqn), `memory`, `gpu`.
- `monitor.py::SystemMonitor` — **통합 시스템 메트릭 수집기**. eBPF와 독립적으로 `/proc`·`/sys`·`nvidia-smi dmon`만 사용해 ARM/x86, GPU 0/1/N, NUMA 유무 무관하게 동작. `start(output_dir, session_id, interval, sys_info)` 호출 시 background thread로 1초 주기 폴링 → `system_metrics_{session_id}.csv` 한 행씩 누적 + `topology_{session_id}.json` 1회 dump. 수집 항목: NUMA node별 CPU%, NVMe 컨트롤러별 IRQ rate + top completion CPU, mem/vm 글로벌 통계, **NUMA node별 메모리 free/used MB**(`/sys/.../node*/meminfo`), GPU(있을 시) power/temp/util, **NUMA node별 CPU 주파수 avg/max MHz**(`/sys/.../cpufreq/scaling_cur_freq`, ARM AMU 즉시값은 일시적 spike(>nominal max) 발생 가능 — raw 보존). `ebpf/io_profiler.py`가 `from core.monitor import SystemMonitor`로 임포트해서 워크로드 thread 옆에 띄운다.

### Result layout

```
results/{YYYYMMDD_HHMMSS}_{tc_name}_{disk_label}/
  metadata.json             # system info + tc 정의
  fio_<workload>.json       # workload 단위 fio raw JSON
  ...
```

Multi-disk 시나리오(`run_all_disks=True`)는 `disk_label="multi_disk"`로 떨어진다. eBPF 결과는 별도로 `ebpf/csv_results/`에 저장된다.

## Conventions when adding/modifying code

- **새 TC 추가**: `test_cases/tcNN_<name>.{json,py}` 명명. `main.py`는 알파벳 정렬로 발견하므로 번호로 실행 순서가 결정된다. Python TC는 반드시 `class Scenario`를 정의하고, 위 시그니처 둘 중 하나를 따른다.
- **fio 호출은 `runner_func`을 통해서만** 한다 (= `bound_runner`). 시나리오가 직접 `subprocess.run`으로 fio를 부르면 quick 모드/fio path override가 깨진다. TC10이 최적화 fio config를 만들 때 예외적으로 직접 부르긴 하지만, 일반 워크로드는 `runner_func`을 쓸 것.
- **결과 저장 패턴**: workload 하나 돌릴 때마다 `reporter.save_json(session_dir, f"fio_{wl_name}.json", result)`. 누적 비교 테이블은 시나리오 인스턴스 변수(`self.all_results`)에 모았다가 디스크별 루프 끝에서 출력하는 식으로 (`tc03_core_distance.py` 패턴 참고).
- **워크로드 dict 키**: `name`, `rw`, `bs`, `iodepth`, `numjobs`, `runtime`, `size`, `cpus_allowed`, `rwmixread`. `runner.py`에서 실제로 fio 플래그로 변환되는 키만 의미가 있다 — `time_based`, `direct`, `group_reporting` 같은 키를 dict에 넣어도 `runner.py`는 무시한다(이미 강제 적용 중).
- **결과 파싱**: fio JSON의 `jobs[0]["read"|"write"]["bw"]`는 KB/s, `clat_ns`/`lat_ns`는 ns. 코드 전반에서 `/1024`로 MB/s, `/1000`으로 us 환산하는 패턴이 일관되게 쓰인다.
- **한국어 출력**: 콘솔 로그/리포트는 전부 한국어. 새 print문도 톤을 맞출 것.

## Known sharp edges

- `core/monitor.py`는 framework `main.py` 파이프라인에서는 자동 호출되지 않고, `ebpf/io_profiler.py`에서만 사용 중. 시나리오에서 쓰려면 명시적으로 `SystemMonitor(...).start()` 호출 필요.
- `scripts/collect_iostat.sh`·`collect_mpstat.sh`는 더 이상 SystemMonitor에서 호출하지 않음 (eBPF가 iostat 대체, mpstat은 `/proc/stat` 직접 폴링으로 대체). 잔존 파일이지만 unused.
- `parser/` 디렉터리는 비어있다 (결과 시각화 파서가 들어갈 자리, 아직 구현 안 됨 — `doc/test_cases_analysis.md` 참고).
- `tc10_multi_ssd_optimal.py`는 최적화 비교군 측정에서 `sudo fio`를 직접 호출한다. `fio_path` 설정이 무시되므로 환경에 따라 깨질 수 있음.
- `config/system.json`의 기본값은 `/dev/ram0..ram3` (RAM 디스크). 실제 NVMe 테스트는 `target_disks`를 비워두고 자동 탐색을 쓰거나 명시적으로 채워 넣는다.
- `doc/test_cases_analysis.md`에 TC05 라벨 버그(QD64로 잘못 표기)가 언급되어 있다 — 코드는 이미 QD1로 동작하지만 출력 라벨이 일부 어긋날 수 있음.
