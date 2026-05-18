# Development Backlog

랄프 루프가 위에서 아래 순서로 처리한다. 각 task는 작게 — 한 커밋 안에 끝낼 수 있어야 함.

**범례**: `P0` 차단/필수 · `P1` 가치 큼 · `P2` nice-to-have · `P3` 발견된 follow-up · `BLOCKED:` 이유

## Pending

### Foundation — eBPF / I/O 정확도 향상

- [ ] **P1** io_uring mode support  **BLOCKED:** 단일 이터레이션 범위 초과 — 신규 BPF 프로그램 2개(io_uring_submit_req/io_uring_complete) + 신규 latency 누적 struct + 신규 글로벌 map + JSON 스키마 확장 + Python parse/리포트 통합 + fio io_uring 검증까지 필요. 아래 sub-task로 분할.
  - 6.11 커널 기준 tracepoint 이름: `io_uring/io_uring_submit_req`(SQE 제출), `io_uring/io_uring_complete`(CQE 푸시), 옵션 `io_uring/io_uring_cqring_wait`.
- [ ] **P2** iouring-1: BPF struct + 2개 tracepoint hook + map
- [ ] **P2** iouring-2: io_trace.c userspace -m iouring 분기 + iouring_overhead JSON
- [ ] **P2** iouring-3: io_profiler.py 통합 + fio --ioengine=io_uring 검증
  - 셋이 묶음 단위. 한 세션에서 연속 작업하는 게 효율적이라 P2로 demote.

### System extensions

- [ ] **P2** nvme controller sysfs stats
  - `/sys/class/nvme/nvme*/model`, `state`, `numa_node`, `queue_count`, `cntrltype`
  - topology.json에 nvme controllers 섹션 확장 (정적 정보)
  - SMART는 별도 task

- [ ] **P2** pcie aer counters (per-device)
  - `/sys/bus/pci/devices/*/aer_dev_correctable`, `aer_dev_fatal` 등
  - 활성화돼 있는 디바이스만 (대부분 0). NVMe 컨트롤러 대상으로만 노출
  - 컬럼: `nvme{N}_aer_correctable, nvme{N}_aer_fatal`

- [ ] **P2** smartctl integration (optional, if smartctl exists)
  - 1회성 metadata + 끝나고 1회 smart attributes dump
  - `which smartctl` 없으면 skip
  - 출력: `smart_{session}.json`

- [ ] **P3** network stats for nvme-of (if applicable)
  - `/proc/net/dev`, ConnectX nic 같은 게 있으면 잡기
  - 일반 시스템엔 noise이므로 explicit opt-in (config 플래그)

### Visualization (단일 HTML 리포트 우선)

- [ ] **P1** system metrics charts (cpu/mem/irq overlay with i/o)
  - 같은 HTML에 system_metrics 차트 추가
  - I/O 차트와 timestamp 동기화 (x축 정렬). 이중 패널 또는 secondary y-axis

- [ ] **P1** lba heatmap chart
  - 64 bucket × time → 2D heatmap (HTML5 canvas 직접 또는 chart.js matrix)
  - 색상: 접근 빈도 log-scale

- [ ] **P1** latency percentile rendering
  - p50/p95/p99/p99.9 시계열 line (A1/A2 완료 의존)
  - 같은 HTML 리포트에 통합

- [ ] **P2** multi-device comparison view
  - 한 세션에 N개 NVMe 있으면 device별 차트를 하나의 그리드에
  - 또는 normalized 한 패널에 overlay

- [ ] **P2** topology svg diagram
  - cpu cores ↔ NUMA nodes ↔ nvme controllers ↔ gpus 단순 SVG
  - inline SVG (외부 라이브러리 안 씀)

### Reporting

- [ ] **P0** markdown summary report
  - `report/md_report.py`: HTML과 동일 입력 → `report_{session}.md`
  - 표 + 핵심 숫자 (총 IOPS, BW, p99, dirty/iowait, GPU peak)
  - "Top findings" 자동 추출 (예: top NUMA node CPU%, top IRQ CPU 등)

- [ ] **P1** session comparison diff
  - `report/diff.py`: 두 session 디렉터리 입력 → 차이 리포트 (md+html)
  - IOPS/BW/lat 통계 비교, regression 후보 highlight (>5% 변동)
  - 검증: 같은 session 두 번 주면 모든 diff가 ~0

- [ ] **P1** json summary export
  - `report/summary.py`: session → `summary_{session}.json` (programmatic consumption)
  - 스키마 평탄화: device별 totals + system 평균/peak + gpu peak

- [ ] **P2** report cli unification
  - `report/__main__.py`: `python3 -m report --session csv_results/ --format html,md,json`
  - 단일 진입점

### Infrastructure

- [ ] **P0** quick smoke script as ci-baseline
  - `scripts/smoke_quick.sh`: 이미 1차 버전 있음. 더 빡세게: 종료 코드 0 보장 + CSV row count > 0 + GPU 컬럼 있는지 체크 (있는 시스템에서)
  - 매 BPF/sysmon 커밋 전 자동 실행 (DEV_RULES에 명시)

- [ ] **P1** root-level cli entry
  - `pmon.py` (or `tools/pmon.py`) — io_profiler.py + report 통합 진입점
  - `pmon run --fio "..."` → 측정 후 자동으로 리포트 생성
  - 인자 design: subcommand `run`, `report`, `diff`

- [ ] **P2** csv/json schema docs
  - `doc/schemas.md` — system_metrics.csv, device CSV, topology.json, summary.json 컬럼 명세
  - 예시 1행 포함

- [ ] **P2** ramdisk fallback for ci-friendly testing
  - 현재 `/tmp/fio_smoke.dat` 쓰는데 디스크 free 적은 시스템 고려
  - tmpfs 명시 + 사이즈 작게 (64M)

### Test framework rewrite

- [ ] **P2** new tc framework draft
  - 기존 `test_cases/*.py` 다 ignore (한 번 backup 후 deprecated/ 로 이동)
  - 신규 `scenarios/` (또는 `test_cases/` 재활용) — 새 SystemMonitor + 통합 리포트 활용하는 베이스 클래스
  - 예시 시나리오 1~2개

- [ ] **P3** scenario: pcie contention (fio + gpu workload)
  - GPU에 가벼운 매트릭스 곱 thread + 동시에 fio → PCIe band 경합 측정

- [ ] **P3** scenario: gc stress with percentile collection
  - Preconditioning → mixed workload → p99 tail 변화 시각화

- [ ] **P2** io_profiler.py: `./io_trace` 상대 경로 → 절대 경로
  - 현재 `subprocess.Popen(["sudo","./io_trace",...])`라 ebpf/ 디렉터리 cwd에서만 동작
  - `os.path.join(os.path.dirname(__file__), "io_trace")` 같이 절대경로화
  - 검증: 프로젝트 루트에서 `python3 ebpf/io_profiler.py ...` 호출이 동작

## Done (newest first)

- [x] **P0** time-series charts (chart.js via cdn)
  - 디바이스별 3개 line chart: IOPS / Bandwidth / D2C latency (op 색 분리)
  - timestamp 통합 정렬, 누락된 op 시점은 null로 align (spanGaps: true)
  - Chart.js v4 CDN via jsdelivr. 오프라인 시 inline JS fallback이
    "Chart.js CDN unreachable" 표시 (table은 정상 렌더 유지)
  - 검증: 3 canvas (iops/bw/d2c) per device + 4 ops × 7 timestamps,
    read는 첫 tick null, flush는 첫 3 tick null로 올바르게 align됨

- [x] **P0** baseline html report generator
  - `report/__init__.py` + `report/html_report.py` 신규
  - `python3 -m report.html_report` 단독 실행, 최신 session_id 자동 탐색
  - 외부 리소스 0 (인터넷 없는 환경 OK), 자기완결 inline CSS
  - 출력: topology 요약 + system_metrics CSV table + device CSV table
  - 검증: 29KB HTML, h1/h2/h3 다 보임, table 정상 렌더

- [x] **P1** per-numa memory stats
  - `_read_numa_meminfo_mb()` + `/sys/.../node*/meminfo` 의 MemFree/MemUsed 파싱
  - 컬럼: `node{N}_mem_free_mb`, `node{N}_mem_used_mb` (단일노드(`all`) 시스템은 글로벌 mem으로 충분 → skip)
  - 검증: GB10 node0 free 106GB / used 16GB 출력

- [x] **P1** cpu frequency tracking (where available)
  - `core/monitor.py`: cpu0..cpuN의 cpufreq sysfs 존재 검사 1회. 있으면 컬럼 `node{N}_freq_{avg,max}_mhz` 추가.
  - 검증: ARM GB10에서 node0 avg ~3300MHz, max 종종 4-7GHz spike (AMU 즉시값 특성).

- [x] **P1** sub-second sampling support
  - io_trace.c: opt_interval double + atof + nanosleep tick. 최소 50ms로 clamp.
  - io_profiler.py: -i argparse type=float
  - 검증: -i 0.5로 ~4초간 8개 JSON 블록 (500ms 주기 정확)

- [x] **P1** record per-request issue cpu + complete cpu (sq/cq divergence stat)
  - `trace_ctx`에 issue_cpu 추가, `block_rq_issue`에서 `bpf_get_smp_processor_id()` 저장
  - `block_rq_complete`에서 현재 CPU 비교 → `device_qd.sq_cq_same`/`sq_cq_diff` atomic 증가
  - JSON에 device-level `sqcq` 객체, CSV에 `sq_cq_diff_ratio` 컬럼, final report에 same/diff% 라인 + NUMA 진단 안내
  - 검증: 4K randread → same 68.9% / diff 31.1% 관찰 (워크로드와 NVMe IRQ CPU 다름)

- [x] **P0** per-interval libaio overhead in csv
  - `prev_libaio` 모듈 dict로 인터벌 delta 추적, `_LIBAIO_OP_KEY` 매핑으로 BPF op↔libaio 필드 연결
  - CSV에 `u2q_avg_us_interval` (글로벌), `c2a_avg_us_interval`/`a2u_avg_us_interval` (op별) 추가
  - 검증: busy 인터벌에서 u2q ~2us, c2a ~1us, a2u ~150us 출력 (read_ahead/discard는 0 — libaio 경로 없음)

- [x] **P0** expose latency histograms in JSON + python percentile calc
  - `compute_percentiles(hist, [50,95,99,99.9])` 헬퍼 추가 (log2 bucket 선형 보간 → us)
  - `print_op_stats`에 `Q2D pct` / `D2C pct` 라인 추가 (operation별)
  - 검증: 4K randread → READ D2C p50=82us, p99=363us, p99.9=685us 출력

- [x] **P0** add latency log2 histograms to BPF (q2d, d2c per type)
  - LAT_HIST_BUCKETS=32, `__builtin_clzll` 대신 수동 unroll loop (BPF target 호환)
  - JSON에 `q2d_hist[32]`, `d2c_hist[32]` 출력 확인 (read d2c bucket 15-17 집중, bucket 23 꼬리 관찰됨)

<!-- 루프가 완료한 task가 여기로 옮겨진다 -->
