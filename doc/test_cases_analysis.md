# Test Cases 일람

`workloads/cases/` 아래에 있는 모든 TC를 코드 기준으로 정리한 표. TC는 파일명 알파벳 순으로 발견되므로 번호가 곧 실행 순서다(`pmon.py monitor --tc all`).

실행 방식:
```bash
./pmon.py monitor --tc tc03        # 이름 부분 매칭 (case-insensitive)
./pmon.py monitor --tc all         # 전체 순차
./pmon.py monitor --tc all -q      # quick: 모든 workload runtime 1초
```

## 현재 TC 목록

| TC ID | 파일 | 형식 | 모드 | 핵심 워크로드 | 측정/목적 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TC00** | `tc00_smoke.json` | JSON | single | Seq Write 128k → Seq Read 128k → Rand Write 4k → Rand Read 4k (각 5s, QD32) | 4대 성능 스모크. write 먼저 → sparse hole 효과 제거. |
| **TC02** | `tc02_dirty_gc.py` | Python | single | 1M Seq Write로 precondition → cache drop → 4K Rand Read QD1 | Dirty 상태에서 GC가 지연시간에 미치는 영향. |
| **TC03** | `tc03_core_distance.py` | Python | single | 전 CPU 코어 순회 × 4K Rand Read QD1 | 코어-디바이스 NUMA distance에 따른 latency 편차. 누적 테이블. |
| **TC04** | `tc04_core_max_iops.py` | Python | single | 전 CPU 코어 순회 × 4K Rand Read QD64 | 단일 코어가 낼 수 있는 최대 IOPS/latency. |
| **TC06** | `tc06_mixed_workload.py` | Python | single | 4K randrw 70/30 × QD 1~256 sweep | 혼합 부하에서 QD별 성능 곡선. |
| **TC07** | `tc07_scalability.py` | Python | **multi** | 4대 패턴(Seq/Rand × R/W) × 디바이스 수 1..N | 장치 수 증가에 따른 성능 선형성. |
| **TC08** | `tc08_3d_sweep.py` | Python | **multi** | (디스크 수 × job 수 × QD) 전수 sweep | 3D 파라미터 공간 전수 조사. |
| **TC09** | `tc09_optimal_config_search.py` | Python | single | NUMA-aware CPU pinning + QD/job 최적 조합 탐색 | 토폴로지를 살린 단일 디스크 최적 구성. |
| **TC10** | `tc10_multi_ssd_optimal.py` | Python | **multi** | 모든 SSD를 묶어 전체 코어로 부하 | 시스템 한계 IOPS/대역폭. |
| **TC11** | `tc11_irq_affinity.py` | Python | single | NVMe IRQ CPU vs 동-NUMA 타 CPU vs 타-NUMA CPU × QD1 4K randread | IRQ affinity mismatch 비용 검출. |

**모드 설명**
- `single`: 디스크별로 시나리오 1회씩 실행 (`disk_label = <device>`).
- `multi`: `run_all_disks = True` — 시나리오가 디스크 리스트 전체를 한 번에 받음 (`disk_label = multi_disk`).

**결번**: TC01, TC05는 현재 존재하지 않는다.

## 파일 형식별 구조

### JSON TC (`tcXX_*.json`)
정적 fio 워크로드 목록. `tc_name`, `description`, `workloads[]`만 있으면 된다. 각 항목이 그대로 `run_fio_job`에 들어간다.

워크로드 dict에서 의미 있는 키 (`fio_runner.py` 기준):
`name`, `rw`, `bs`, `iodepth`, `numjobs`, `runtime`, `size`, `cpus_allowed`, `rwmixread`, `ioengine` (기본 `libaio`; io_uring 분석은 `io_uring`).
`time_based`/`direct`/`group_reporting`은 강제 적용되므로 dict에 넣어도 무시된다.

### Python TC (`tcXX_*.py`)
`class Scenario`를 export. 시그니처 둘 중 하나:
- **single-disk** (기본): `execute(self, disk, runner_func, reporter, session_dir, numa_node, sys_info=None)`
- **multi-disk**: 클래스에 `self.run_all_disks = True`. `execute(self, disks, ...)` — 디스크 리스트 전체를 받는다.

NUMA/CPU 토폴로지 기반 최적화(TC08/09/10/11)는 `sys_info["discovered"]`에 의존하므로 새 TC도 `sys_info=None`을 시그니처에 받는다.

## 알려진 이슈

- **TC10**: 최적화 비교군 측정에서 `sudo fio`를 직접 호출. `fio_path` 설정이 무시되므로 환경에 따라 깨질 수 있음.
- 새 TC 추가 시 번호는 다음 비어있는 슬롯(TC01, TC05, TC12...) 또는 끝에 잇는다. 결번을 채울지는 자유.
