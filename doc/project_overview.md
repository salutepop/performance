# 시스템 관측 & SSD 성능 분석 프레임워크

이 프로젝트는 **시스템 관측(monitoring)**을 1차 목적으로 한다. 어떤 시스템/워크로드가 돌지 모르는 상황에서 모니터링 도구로 데이터를 수집하는 게 핵심이고, 시스템 병목을 분석하기 위한 워크로드(`fio` 기반 test case)를 돌리는 건 2차 목적이다.

## 🎯 목적
- **1차 — 관측**: SystemMonitor(/proc·/sys·nvidia-smi) + eBPF block-layer I/O tracer로 시스템 상태를 세션 단위로 수집. 워크로드가 없어도 그냥 관측 가능.
- **2차 — 분석용 워크로드**: BW/IOPS/Latency를 다양한 조건(GC, NUMA distance, core affinity 등)에서 측정해 병목을 드러낸다.

## 📁 프로젝트 구조
- **`pmon.py`**: 유일한 진입점. `monitor` / `report` / `diff` / `summary` / `debug` 서브커맨드.
- **`monitoring/`**: 관측 플랫폼 (1차).
  - `session.py`: `Session` — 관측 윈도우(컨텍스트 매니저). collector들을 구동하고 종료 시 리포트.
  - `discovery.py`: `SystemDiscovery` — CPU/NUMA/NVMe/메모리/GPU 자동 탐색.
  - `collectors/`: 관측 소스. `base.py`(Collector ABC), `system.py`(SystemMonitor), `ebpf_io/`(eBPF I/O tracer).
- **`workloads/`**: 워크로드 (2차, 관측의 옵션 입력).
  - `fio_runner.py`: `fio` 명령 빌드/실행.
  - `reporter.py`: TC 결과 디렉터리 layout.
  - `tc_runner.py`: test case 발견/실행.
  - `cases/`: test case 본체 (`.json` 정적 워크로드 / `.py` 동적 시나리오).
  - `scenarios/`: self-checking Scenario 프레임워크.
- **`report/`**: 세션 산출물 → MD/JSON/PNG/PDF 리포트.
- **`config/`**: 시스템 설정 (`system.json`).
- **`results/`**: 세션 산출물 저장소.

## 🚀 주요 기능
1. **워크로드 무관 관측**: `pmon.py monitor --duration N`으로 아무 워크로드 없이 시스템을 관측.
2. **collector 확장성**: `Collector` ABC를 상속해 새 관측 소스를 추가.
3. **JSON/Python test case**: 정적 워크로드와 동적 시나리오 모두 지원.
4. **NUMA / CPU Affinity 제어**: NUMA 노드 설정 및 코어 할당.
5. **세션 기반 결과 관리**: 실행마다 타임스탬프 디렉터리.

## 🛠 사용 방법
1. `config/system.json`에서 타겟 디스크(`target_disks`)와 `fio` 경로를 설정 (비워두면 자동 탐색).
2. 관측 실행:
   ```bash
   ./pmon.py monitor --duration 60          # 워크로드 없이 관측
   ./pmon.py monitor --tc tc03              # test case와 함께
   ./pmon.py monitor --tc all -q            # 전체 TC, quick 모드
   ```

## 📈 테스트 시나리오 예시
- `tc00_smoke.json`: 4대 성능(Seq/Rand × R/W) 스모크.
- `tc02_dirty_gc.py`: SSD를 Dirty 상태로 만든 후 Garbage Collection의 영향을 측정.
- `tc03_core_distance.py`: CPU 코어와 SSD 간 거리에 따른 성능 차이 분석.
