# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

시스템 관측(monitoring)이 1차 목적인 프레임워크. 어떤 시스템/워크로드가 돌지 모르는 상황에서 모니터링 도구로 데이터를 수집하는 게 핵심이고, 시스템 병목을 분석하기 위해 필요한 워크로드(test case)를 돌리는 건 2차 목적이다.

- **1차 — 관측 플랫폼 (`monitoring/`)**: SystemMonitor(/proc·/sys·nvidia-smi) + eBPF block-layer I/O tracer. 워크로드가 없어도 그냥 시스템을 관측할 수 있다.
- **2차 — 워크로드 (`workloads/`)**: `fio` 기반 test case. 병목을 유도/측정하기 위한 입력일 뿐. 관측 Session 안에서 실행된다.

설명/주석은 한국어가 기본. 단, **사용자 facing 출력(리포트/차트/콘솔)은 영어**로 유지한다.

## Common commands

진입점은 프로젝트 루트의 `pmon.py` 하나뿐이다.

```bash
# 관측만 (워크로드 없음)
./pmon.py monitor --duration 60
./pmon.py monitor --duration 0            # Ctrl-C로 종료

# 관측 + ad-hoc 워크로드
./pmon.py monitor --fio "fio --name=t --filename=/tmp/x --rw=randread --bs=4k ..."
./pmon.py monitor --script monitoring/collectors/ebpf_io/src/fio.sh --ebpf on

# 관측 + test case
./pmon.py monitor --tc tc03               # 이름 부분 매칭 (case-insensitive)
./pmon.py monitor --tc all                # 전체 TC 순차 실행
./pmon.py monitor --tc tc06 -q            # quick 모드: 모든 워크로드 runtime 1초

# 사후 처리
./pmon.py report                          # 가장 최근 세션에서 리포트만
./pmon.py diff --baseline SID --candidate SID
./pmon.py summary                         # 평탄화 JSON export

# 개발 검증 (코드 수정 후 자체 검증용)
./pmon.py debug                           # 4-phase fio + eBPF + 전체 리포트 E2E, PASS/FAIL 종료코드
./pmon.py debug --duration 1              # 워크로드 페이즈당 1초로 단축 (기본 3초)
```

NVMe raw 디바이스에 직접 쓰는 워크로드가 많아 root/sudo 권한이 거의 항상 필요하다. `tc02_dirty_gc.py` 같은 케이스는 `/proc/sys/vm/drop_caches`에 쓴다.

### Python 환경 (uv)

[`uv`](https://docs.astral.sh/uv/)로 환경 관리. 외부 deps는 matplotlib 하나(PNG/PDF 리포트용)이고 나머지는 stdlib. `pyproject.toml` + `uv.lock`으로 재현성 보장. `uv sync` 한 번 → `uv run ./pmon.py ...` 또는 `python3 ./pmon.py ...` 둘 다 동작. 자세한 사용법은 [`doc/uv_usage.md`](./doc/uv_usage.md).

## Folder structure

```
performance/
  pmon.py                              # 유일한 진입점 (monitor/report/diff/summary/debug)
  monitoring/                          # ★ 1차 — 관측 플랫폼
    __init__.py                        #   exports: Session, SystemDiscovery, ...
    session.py                         #   Session 컨텍스트 매니저 + resolve_ebpf_mode/default_collectors
    discovery.py                       #   SystemDiscovery
    collectors/
      base.py                          #   Collector ABC (start/stop 계약)
      system.py                        #   SystemMonitor (/proc·/sys·nvidia-smi 폴러)
      system_collector.py              #   SystemCollector — SystemMonitor를 Collector로 어댑트
      ebpf_io/                         #   eBPF block-layer I/O collector
        __init__.py                    #     EbpfIoCollector, ebpf_available()
        collector.py                   #     io_trace를 구동하는 Python orchestrator
        CLAUDE.md                      #     eBPF 서브시스템 상세 문서
        src/                           #     C/BPF 소스 + Makefile + vmlinux.h
  workloads/                           # ★ 2차 — 워크로드 (관측의 옵션 입력)
    fio_runner.py                      #   run_fio_job — fio 명령 빌드/실행
    reporter.py                        #   ResultReporter — TC 결과 디렉터리 layout
    tc_runner.py                       #   run_test_cases — TC 발견/실행 (pmon monitor --tc의 본체)
    cases/                             #   test case 본체 (tcXX_*.json / tcXX_*.py)
    scenarios/                         #   self-checking Scenario 프레임워크 (별개)
  report/                              # 세션 산출물 → MD/JSON/PNG/PDF 리포트
  config/                              # system.json (target_disks, fio_path 등)
  doc/                                 # 문서
  results/                             # 세션 산출물 저장소
```

## Architecture

### Session — 관측의 단위 (`monitoring/session.py`)

`Session`은 컨텍스트 매니저다. 디스크 위의 디렉터리 하나 + 그 안으로 데이터를 쓰는 collector들의 묶음. 워크로드는 Session 컨텍스트 *안에서* 실행되고, Session은 무엇이 실행되는지 신경 쓰지 않는다 — 관측 윈도우를 올바르게 열고 닫을 뿐.

```python
from monitoring import Session
from monitoring.collectors import SystemCollector, EbpfIoCollector

with Session(session_dir, sys_info,
             collectors=[SystemCollector(), EbpfIoCollector()],
             reports="all"):
    run_workload(...)            # I/O를 일으키는 무엇이든
# 종료 시: collector 역순 stop → 리포트 생성
```

`collectors=`를 생략하면 `ebpf_mode` 인자로부터 기본 리스트(`default_collectors`)를 만든다 — 이게 backward-compat 경로. 종료 시 collector는 시작 역순으로 stop되고(eBPF tracer가 SystemMonitor보다 먼저), 그 다음 `reports` 포맷대로 리포트를 렌더한다.

### Collector — 관측 소스 추상화 (`monitoring/collectors/base.py`)

`Collector` ABC: `name` + `start(session_dir, session_id, sys_info)` / `stop()`. 새 관측 소스(네트워크 통계, perf 카운터, 다른 eBPF tracer)를 추가하려면 `Collector`를 상속하고 두 메서드를 구현하면 된다. `start()`는 raise하면 안 된다 — collector는 best-effort라 실패해도 경고만 찍고 나머지 세션은 정상 진행.

현재 구현체:
- `SystemCollector` (`system_collector.py`) — `SystemMonitor`를 감싼다.
- `EbpfIoCollector` (`ebpf_io/__init__.py`) — io_trace orchestrator(`collector.py`)를 subprocess로 띄운다. `--no-sysmon`으로 — SystemMonitor는 Session이 소유하므로 중복 방지.

### Test-case 실행 파이프라인 (`workloads/tc_runner.py`)

`pmon.py monitor --tc`가 `run_test_cases()`를 호출한다:

1. `SystemDiscovery`가 CPU/NUMA/NVMe/메모리 정보를 자동 탐색해 `config/discovered_system.json`에 떨어뜨린다.
2. `config/system.json`을 로드하고, `target_disks`가 비어있으면 발견된 NVMe namespace로 자동 채운다. 발견 정보는 `sys_info["discovered"]`에 병합되어 모든 시나리오로 전달된다.
3. `workloads/cases/*.json` + `*.py`를 정렬해 순회 (번호로 실행 순서 결정).
4. `functools.partial`로 `run_fio_job`에 `fio_path`/`runtime_override`를 미리 묶어 `bound_runner`를 만들고 시나리오에 주입. 시나리오는 fio 경로/quick 모드를 신경 쓸 필요 없다.
5. 각 TC는 자기만의 `Session` 안에서 실행된다. `.json` TC는 `_execute_json_tc`가 워크로드 리스트를 순회, `.py` TC는 `_execute_python_tc`가 `importlib`로 동적 로드 후 `Scenario`를 인스턴스화해 `execute(...)`를 호출.

### Two TC formats (`workloads/cases/`)

- **JSON (`tcXX_*.json`)**: 정적 fio 워크로드 목록. `tc_name`, `description`, `workloads[]`만 있으면 된다. `workloads`의 각 항목이 그대로 `run_fio_job`에 들어간다.
- **Python (`tcXX_*.py`)**: `class Scenario`를 export해야 한다. 동적 제어(전처리 → 측정 → 조건부 분기, 코어 순회, 누적 테이블 등)에 사용. 시그니처 둘 중 하나:
  - 단일 디스크 모드 (기본): `execute(self, disk, runner_func, reporter, session_dir, numa_node, sys_info=None)`.
  - Multi-disk 모드: `self.run_all_disks = True`로 표시하면 디스크 리스트 전체를 한 번에 넘긴다 → `execute(self, disks, ...)`.

`sys_info` 인자는 NUMA/CPU 토폴로지 기반 최적화(TC08/09/10)에 필수. 새 TC는 `sys_info=None`을 시그니처에 받도록 한다.

### Scenario 프레임워크 (`workloads/scenarios/`)

TC와 별개인 self-checking 워크로드 프레임워크. `Scenario` 하위 클래스가 `fio_cmd()`와 (선택) `analyze(summary_json)`을 override. `.run()`은 `pmon.py monitor`를 subprocess로 호출한 뒤 `results/` 아래 최신 `summary_*.json`을 분석해 `{pass: bool, ...}`를 반환.

```python
from workloads.scenarios.base import Scenario
class MyTest(Scenario):
    name = "my_test"
    def fio_cmd(self): return "fio --name=... ..."
    def analyze(self, summary):
        d2c = summary["devices"]["nvme0n1"]["ops"]["read"]["d2c_us_avg"]
        return {"pass": d2c < 200}
```

실행: `python3 -m workloads.scenarios.<module>`. 종료 코드는 `analyze()`의 `pass` 키를 따름. 예시: `workloads/scenarios/sample_randread.py`.

### eBPF I/O 트레이서 (`monitoring/collectors/ebpf_io/`)

> eBPF 서브시스템은 별도 문서가 있다: [`monitoring/collectors/ebpf_io/CLAUDE.md`](./monitoring/collectors/ebpf_io/CLAUDE.md). 이 폴더 코드를 만질 때는 그 문서를 먼저 읽을 것.

```bash
# 빌드 — ARM/x86 공용 헬퍼 (의존성 점검 + 클린 빌드). 권장 진입점.
./scripts/build_ebpf.sh
./scripts/build_ebpf.sh --deps                     # 빌드 의존성을 apt로 설치(root) 후 빌드
./scripts/build_ebpf.sh --check                    # 의존성/환경만 점검

# 직접 make (개발 edit 루프용)
make -C monitoring/collectors/ebpf_io/src
make -C monitoring/collectors/ebpf_io/src clean
make -C monitoring/collectors/ebpf_io/src distclean # clean + vmlinux.h (아키텍처 전환 후)
make -C monitoring/collectors/ebpf_io/src smoke    # = pmon.py debug

# 단독 실행 (엔진/transport 자동탐지 — mode 인자 없음)
python3 monitoring/collectors/ebpf_io/collector.py -i 1 -c "fio ..."
```

빌드 의존성: `clang`, `make`, `gcc`, `bpftool`, `libbpf-dev`, `libelf-dev`, `zlib1g-dev`. `vmlinux.h`와 `io_trace.skel.h`는 git에 추적되지 않고 빌드 시 현재 커널 BTF에서 생성된다 — `scripts/build_ebpf.sh`가 매번 새로 뽑으므로 ARM↔x86 이전 후에도 그냥 재빌드하면 된다.

### 리포트 생성 (`report/`)

세션 산출물(`topology_*.json`, `system_metrics_*.csv`, `<device>_*.csv`)을 자기완결 리포트로 변환. session-id 생략 시 가장 최근 `topology_*.json` 자동 선택. Session이 종료 시 자동 호출하지만 `python3 -m report.X`로 단독 호출도 가능.

- **Markdown** (`report.md_report`) — Top findings + Topology/Device/System aggregate 요약 (~1.5KB 포터블 텍스트).
- **JSON summary** (`report.summary`) — 단일 평탄화 JSON export (`summary_{sid}.json`). 스키마 stable — 컬럼 추가만 허용.
- **PNG + Markdown** (`report.png_report`) — matplotlib(Agg)로 차트를 PNG로 렌더, markdown이 참조. 오프라인/GUI 없는 서버용.
- **PDF** (`report.pdf_report`) — 표지(markdown 구조 렌더) + PNG 차트 페이지를 묶은 단일 PDF.
- **Session diff** (`report.diff`) — 두 세션 aggregate 비교. 변동 ≥5% ⚠, ≥20% ⛔.
- 공통 데이터 로딩/시리즈 빌더는 `report.datasource`에 모여 있다 (렌더링 무관 순수 데이터 레이어).

### Result layout

```
results/run_{YYYYMMDD_HHMMSS}/{tc_name}_{disk_label}/   # TC 실행 (pmon monitor --tc)
results/{YYYYMMDD_HHMMSS}_monitor[_{label}]/            # ad-hoc 관측 (pmon monitor)
  metadata.json             # system info + tc/monitor 정의
  topology_{sid}.json       # SystemMonitor 1회 dump
  system_metrics_{sid}.csv  # SystemMonitor 1초 주기 누적
  {device}_{sid}.csv        # eBPF I/O 트레이서 timeseries (디바이스별)
  ebpf_analysis_{sid}.txt   # eBPF 트레이서 stdout raw 캡처 (full-stack 표 등)
  ebpf_summary_{sid}.json   # eBPF 구조화 요약 (차트 입력 — phase latency, size dist)
  fio_{workload}.json       # workload 단위 fio raw JSON (TC만)
  report_{sid}.md / .pdf    # 리포트 (+ summary_{sid}.json, report_png_{sid}.md, figs_{sid}/)
```

Multi-disk 시나리오(`run_all_disks=True`)는 `disk_label="multi_disk"`로 떨어진다.

## Conventions when adding/modifying code

- **새 TC 추가**: `workloads/cases/tcNN_<name>.{json,py}` 명명. 알파벳 정렬로 발견하므로 번호가 실행 순서. Python TC는 반드시 `class Scenario`를 정의하고 위 시그니처 둘 중 하나를 따른다.
- **새 collector 추가**: `monitoring/collectors/`에 `Collector` ABC를 상속한 클래스. `Session(collectors=[...])`에 넣으면 끝.
- **fio 호출은 `runner_func`을 통해서만** 한다 (= `bound_runner`). 시나리오가 직접 `subprocess.run`으로 fio를 부르면 quick 모드/fio path override가 깨진다. TC10이 예외적으로 직접 부르지만, 일반 워크로드는 `runner_func`을 쓸 것.
- **결과 저장 패턴**: workload 하나 돌릴 때마다 `reporter.save_json(session_dir, f"fio_{wl_name}.json", result)`. 누적 비교 테이블은 시나리오 인스턴스 변수(`self.all_results`)에 모았다가 디스크별 루프 끝에서 출력 (`tc03_core_distance.py` 패턴 참고).
- **워크로드 dict 키**: `name`, `rw`, `bs`, `iodepth`, `numjobs`, `runtime`, `size`, `cpus_allowed`, `rwmixread`, `ioengine` (기본 `libaio`; io_uring 분석 시 `io_uring`). `fio_runner.py`에서 실제 fio 플래그로 변환되는 키만 의미 있다 — `time_based`, `direct`, `group_reporting`은 dict에 넣어도 무시된다(이미 강제 적용).
- **결과 파싱**: fio JSON의 `jobs[0]["read"|"write"]["bw"]`는 KB/s, `clat_ns`/`lat_ns`는 ns. 코드 전반에서 `/1024`로 MB/s, `/1000`으로 us 환산.
- **출력 언어**: 콘솔 로그/리포트 등 사용자 facing 출력은 영어. 코드 주석/내부 문서는 한국어 유지 가능.

## Known sharp edges

- **eBPF는 sudo NOPASSWD 경로에 의존**한다. `monitoring/collectors/ebpf_io/src/io_trace` 바이너리가 sudoers의 NOPASSWD 룰에 등록돼 있어야 `pmon.py debug` / `--ebpf on`이 동작한다 (DEV_RULES.md 참고). 바이너리를 옮기면 sudoers도 갱신해야 한다.
- `tc10_multi_ssd_optimal.py`는 최적화 비교군 측정에서 `sudo fio`를 직접 호출한다. `fio_path` 설정이 무시되므로 환경에 따라 깨질 수 있음.
- `config/system.json`의 기본값은 `/dev/ram0..ram3` (RAM 디스크). 실제 NVMe 테스트는 `target_disks`를 비워 자동 탐색을 쓰거나 명시적으로 채운다.
- `doc/test_cases_analysis.md`에 TC05 라벨 버그(QD64로 잘못 표기)가 언급돼 있다 — 코드는 QD1로 동작하지만 출력 라벨이 어긋날 수 있음.
- `report.png_report` / `report.pdf_report`는 matplotlib 필요. 미설치 시 자동 스킵 (`report.__main__`이 ImportError를 잡아 다른 포맷만 생성).
