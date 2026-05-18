# Development Backlog

랄프 루프가 위에서 아래 순서로 처리한다. 각 task는 작게 — 한 커밋 안에 끝낼 수 있어야 함.

**범례**: `P0` 차단/필수 · `P1` 가치 큼 · `P2` nice-to-have · `P3` 발견된 follow-up · `BLOCKED:` 이유

## Pending

### Foundation — eBPF / I/O 정확도 향상

- [ ] **P1** io_uring mode support  **BLOCKED:** 단일 이터레이션 범위 초과 — 신규 BPF 프로그램 2개(io_uring_submit_req/io_uring_complete) + 신규 latency 누적 struct + 신규 글로벌 map + JSON 스키마 확장 + Python parse/리포트 통합 + fio io_uring 검증까지 필요. 아래 sub-task로 분할.
  - 6.11 커널 기준 tracepoint 이름: `io_uring/io_uring_submit_req`(SQE 제출), `io_uring/io_uring_complete`(CQE 푸시), 옵션 `io_uring/io_uring_cqring_wait`.
- [ ] **P3** md_report: D2C/Q2D avg를 iops 가중평균으로 (현재 단순 mean이 첫 인터벌 outlier에 끌림)
- [ ] **P3** html/md report에 nvme_ctrls 상세(model/firmware/queue_count) 표시 — 데이터는 이미 topology.json에 캡처됨
- [ ] **P2** iouring-1: BPF struct + 2개 tracepoint hook + map
- [ ] **P2** iouring-2: io_trace.c userspace -m iouring 분기 + iouring_overhead JSON
- [ ] **P2** iouring-3: io_profiler.py 통합 + fio --ioengine=io_uring 검증
  - 셋이 묶음 단위. 한 세션에서 연속 작업하는 게 효율적이라 P2로 demote.

### System extensions

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

- [ ] **P2** multi-device comparison view
  - 한 세션에 N개 NVMe 있으면 device별 차트를 하나의 그리드에
  - 또는 normalized 한 패널에 overlay

- [ ] **P2** topology svg diagram
  - cpu cores ↔ NUMA nodes ↔ nvme controllers ↔ gpus 단순 SVG
  - inline SVG (외부 라이브러리 안 씀)

### Reporting

- [ ] **P2** report cli unification
  - `report/__main__.py`: `python3 -m report --session csv_results/ --format html,md,json`
  - 단일 진입점

### Infrastructure

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

- [x] **P2** nvme controller sysfs stats
  - core/discovery.py에 `_discover_nvme_ctrls` 추가. 10개 attr 수집:
    model/state/firmware_rev/serial/transport/address/cntrltype/queue_count/numa_node/subsysnqn
  - topology.json의 raw.discovered.nvme_ctrls에 노출됨 (post-hoc 분석 컨텍스트)
  - 검증: Samsung MZALC4T0HBL1 / firmware NXHB202Q / queue_count=16 / state=live 캡처

- [x] **P1** root-level cli entry
  - 프로젝트 루트에 `pmon.py` 신규. subcommand 4종: run / report / diff / summary.
  - `run`: io_profiler 호출 후 자동 리포트 생성 (--report all|html|md|json|none)
  - `report`: 기존 세션 산출물에서 html+md+json 일괄 생성 (--format 콤마구분)
  - `diff`, `summary`: report.diff / report.summary 모듈 위임
  - 검증: 4 subcommand 전부 정상 동작 (run 후 26KB HTML 자동 생성 확인)

- [x] **P1** json summary export
  - `report/summary.py` 신규. md_report aggregate를 평탄한 stable JSON 스키마로 변환
  - 구조: topology + devices{name: {sqcq, ops: {op: {total_io, bw/d2c/qd peaks}}}}
    + system {cpu per-node, memory peaks, nvme_irq, gpu peaks}
  - 검증: 2.2KB JSON, 모든 device/system 필드 정상 출력

- [x] **P1** session comparison diff
  - `report/diff.py` 신규. md_report의 _device_aggregates/_system_aggregates 재사용
  - CLI: --baseline / --candidate (SID 또는 폴더 경로). --session-dir 옵션
  - 출력: op별 IOPS/BW/D2C/peak QD, system per-node CPU%/IRQ rate/GPU peak 변동률.
    Marker: ⚠ |Δ|≥5%, ⛔ |Δ|≥20%, baseline 0 또는 None은 marker 없음
  - 검증: same-same → 모든 변동 +0.0% (legend 외 marker 0건);
    다른 세션 → 10개 marker (read_ahead BW -60%/D2C +470% ⛔ 등 정확히 잡힘)

- [x] **P1** latency percentile rendering
  - io_profiler.py: prev_hists dict로 (dev,op,phase)별 누적 histogram 추적,
    인터벌 delta에 compute_percentiles 적용. CSV 신규 컬럼 d2c_p50_us,
    d2c_p99_us, q2d_p99_us 추가
  - html_report.py: device 차트 spec에 p50/p99 추가 (총 5개 차트:
    iops/bw/d2c avg/p50/p99). p50/p99 데이터 없는 구버전 CSV는 자동 생략
  - 검증: 4K randrw → 정상 인터벌 read p99=256us, write p99=150us
    (첫 baseline은 preconditioning outlier로 16ms)

- [x] **P1** lba heatmap chart
  - Device별 (LBA bucket × timestamp) 2D heatmap. HTML5 canvas 직접 그림 (Chart.js
    matrix plugin 의존성 없음 — code-size 작음).
  - 누적 lba_N → 인터벌 delta 변환 (op 무관 합계). log-scale viridis 색.
  - 검증: bucket 18 (256MB 파일 위치)에 200K/인터벌 집중 관찰됨

- [x] **P1** system metrics charts (cpu/mem/irq overlay with i/o)
  - HTML "System metrics" 섹션에 4개 line chart 추가: CPU %(per NUMA), NVMe IRQ/s,
    Memory dirty/writeback, GPU SM%/power
  - 각 차트는 조건부 — 데이터 없으면 canvas 생략 (single-node, no-GPU 시스템 대응)
  - 검증: GB10 세션 7 canvas total (system 4 + device 3) 정상

- [x] **P0** quick smoke script as ci-baseline (강화판)
  - 7단계 검증으로 확장:
    1) BPF 빌드, 2) io_profiler smoke, 3) 산출물 존재/행수, 4) topology JSON 스키마,
    5) conditional GPU 컬럼 (topology에 GPU 있으면 sys CSV에도 있어야), 6) QD sanity,
    7) report 생성 (HTML+MD) — HTML에 canvas + chart.js script 필수
  - 검증: 7/7 단계 PASS, has_gpu=1 시 GPU 컬럼 확인됨

- [x] **P0** markdown summary report
  - `report/md_report.py` 신규. html_report와 같은 loader 재사용
  - Top findings auto-extract: SQ/CQ diff, iowait peak, dirty mem, GPU 활동, 가장 바쁜 NUMA node
  - Device I/O aggregate (op별 총 IO/peak BW/avg lat/peak QD), CPU per-node, mem/vm,
    NVMe IRQ rate, GPU peak — 5 markdown 테이블
  - 검증: GB10 세션에서 1.5KB MD 출력, 3개 finding 자동 생성

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
