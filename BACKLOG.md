# Development Backlog

랄프 루프가 위에서 아래 순서로 처리한다. 각 task는 작게 — 한 커밋 안에 끝낼 수 있어야 함.

**범례**: `P0` 차단/필수 · `P1` 가치 큼 · `P2` nice-to-have · `P3` 발견된 follow-up · `BLOCKED:` 이유

## Pending

### Foundation — eBPF / I/O 정확도 향상

- [ ] **P1** io_uring mode support  **BLOCKED:** 단일 이터레이션 범위 초과 — 신규 BPF 프로그램 2개(io_uring_submit_req/io_uring_complete) + 신규 latency 누적 struct + 신규 글로벌 map + JSON 스키마 확장 + Python parse/리포트 통합 + fio io_uring 검증까지 필요. 아래 sub-task로 분할.
  - 6.11 커널 기준 tracepoint 이름: `io_uring/io_uring_submit_req`(SQE 제출), `io_uring/io_uring_complete`(CQE 푸시), 옵션 `io_uring/io_uring_cqring_wait`.
- [ ] **P2** iouring-1: BPF struct + 2개 tracepoint hook + map  **BLOCKED:** io_uring 워크로드(fio --ioengine=io_uring) 검증 인프라 필요. 셋이 묶음 단위라 연속 작업하는 게 효율적.
- [ ] **P2** iouring-2: io_trace.c userspace -m iouring 분기 + iouring_overhead JSON  **BLOCKED:** iouring-1 의존
- [ ] **P2** iouring-3: io_profiler.py 통합 + fio --ioengine=io_uring 검증  **BLOCKED:** iouring-1/2 의존

### System extensions

- [ ] **P2** smartctl integration (optional, if smartctl exists)  **BLOCKED:** smartctl가 NVMe 블록 디바이스 접근에 root 필요. 현재 NOPASSWD sudoers에 미포함이라 자율 루프에서 검증 불가.


### Visualization (단일 HTML 리포트 우선)

### Reporting

### Infrastructure

### Test framework rewrite

- [ ] **P3** scenario: pcie contention (fio + gpu workload)
  - GPU에 가벼운 매트릭스 곱 thread + 동시에 fio → PCIe band 경합 측정

- [ ] **P3** scenario: gc stress with percentile collection
  - Preconditioning → mixed workload → p99 tail 변화 시각화


## Done (newest first)

- [x] **P3** network stats for nvme-of (opt-in)
  - core/monitor.py: PMON_ENABLE_NET=1 환경변수로 활성. 기본은 비활성 (일반
    시스템에서 노이즈 회피).
  - /proc/net/dev 파싱 + interval delta → net_<iface>_{rx,tx}_mb_s 컬럼
  - lo/docker/br-/veth/virbr 자동 제외 (물리/RDMA NIC만 남김)
  - 검증: env 미설정 → 0 net 컬럼; PMON_ENABLE_NET=1 → 4 iface × 2 (rx/tx)
    = 8 net 컬럼 (enP7s7, enp1s0f0np0 등 정상 인식)

- [x] **P3** svg topology nvme_ctrls path fix
  - `_render_topology_svg`: raw.nvme_ctrls (flat) 우선, raw.discovered.nvme_ctrls fallback.
  - 이전엔 flat 구조에서 NUMA 매핑 lookup 실패 → 디바이스→노드 edge 미연결.
  - 검증: GB10 환경에서 nvme0 NUMA=-1 정확히 식별 (실제 unified memory 값).

- [x] **P3** html/md report에 nvme_ctrls 상세 표시
  - html_report._render_topology: NUMA 노드 dl 아래에 nvme 상세 테이블 추가
    (ctrl/model/firmware/queue_count/state/transport/numa). topo.raw.nvme_ctrls
    + raw.discovered.nvme_ctrls 모두 시도 (양 포맷 호환).
  - md_report.build_report Topology 섹션에 동일 테이블.
  - 검증: GB10 Samsung MZALC4T0HBL1 / NXHB202Q / queue=16 / live / pcie 정상 표시.

- [x] **P3** md_report iops-weighted D2C/Q2D avg
  - md_report._device_aggregates: weighted_rows로 (iops, q2d, d2c) parallel tuple
    수집 → _weighted_lat()로 iops-가중평균 계산 → q2d/d2c "avg" 키 override.
  - 검증: 인터벌 outlier가 있던 old session에서 write d2c
    simple=714us → weighted=49.5us (14배 차이, 100K IOPS interval이 제대로 반영).

- [x] **P2** io_profiler.py io_trace 경로 절대화
  - `sudo ./io_trace` → `sudo /abs/path/io_trace` (os.path.dirname(__file__) 기준)
  - 프로젝트 루트에서 `python3 ebpf/io_profiler.py ...` 호출도 동작
  - sudoers NOPASSWD가 절대 경로로 매칭되므로 호환
  - 검증: 루트 cwd에서 실행 → exit 0, FINAL REPORT 출력, BW 212 MB/s

- [x] **P2** new tc framework draft
  - 신규 `scenarios/` 디렉터리. 기존 test_cases/*.py는 그대로 유지 (deprecated).
  - `scenarios/base.py`: `Scenario` 베이스 — `fio_cmd()` + `analyze()` override.
    내부적으로 `pmon.py run` 호출 후 summary_<sid>.json 분석.
    `analyze()` 반환의 `pass: bool`이 종료 코드에 반영 (CI 친화).
  - `scenarios/sample_randread.py`: 5s 4K randread → d2c_avg < 1ms + total_io > 1000 검증.
  - 검증: `python3 -m scenarios.sample_randread` → total_io=264K, d2c=110us, pass=True, exit=0

- [x] **P2** ci-friendly smoke parameters
  - smoke_quick.sh: SIZE 256M → 64M 기본값, env로 override 가능 (SIZE/NUMJOBS/TMP_FIO/RUNTIME)
  - 시작 시 df로 free space 체크, SIZE의 2배 미만이면 WARN
  - KEEP_FIO=0 으로 종료 시 fio test file cleanup (기본은 재사용을 위해 유지)
  - 주: tmpfs (/dev/shm)는 fio --direct=1 미지원이라 사용 불가 → /tmp ext4 유지
  - 검증: 3 조합 (default / SIZE=32M / KEEP_FIO=0) 전부 PASS

- [x] **P2** csv/json schema docs
  - `doc/schemas.md` 신규. 5개 산출물 명세:
    - `<device>_*.csv` (이전에 ebpf/CLAUDE.md에 흩어져 있던 컬럼 정리)
    - `system_metrics_*.csv` (동적 컬럼 패턴 documentation)
    - `topology_*.json` (raw가 flat — `raw.nvme_ctrls` 직접 경로임을 명시)
    - `summary_*.json` (stable schema)
    - report HTML/MD/diff 요약
  - add-only 정책 명시 (외부 도구 호환)
  - 발견된 P3: html_report._render_topology_svg의 nvme_ctrls 경로 오류

- [x] **P2** report cli unification (`python3 -m report`)
  - report/__main__.py 신규. --format all|html,md,json 콤마구분 옵션
  - 내부적으로 html_report.main / md_report.main / summary.main 호출
  - diff는 분리 — `python3 -m report.diff` 또는 pmon.py diff 사용
  - 검증: 모든 포맷 콤보 (`all`, `html`, `md,json`) 정상 동작

- [x] **P2** topology svg diagram
  - report/html_report.py: `_render_topology_svg(topo)` 신규. inline SVG로
    NUMA node 박스 + 그 아래 CPU range 라벨 + 디바이스 row(NVMe/GPU 박스).
    디바이스의 numa_node가 NUMA 노드와 매칭되면 line으로 연결, 아니면 (예: -1
    unified memory) 미연결 — 정확히 시각화.
  - 외부 라이브러리 0, CSS는 SVG 안 inline 스타일.
  - 검증: GB10 → 3 box(1 NUMA + 1 NVMe + 1 GPU), 6 text label, 0 line
    (NVMe/GPU 모두 numa_node=-1 정확 반영)

- [x] **P2** multi-device comparison view
  - HTML 리포트 Device I/O 섹션 맨 위에 "Overview" 패널 추가.
  - 디바이스별 (op 합산) IOPS / BW 시계열을 한 차트에 색 분리해 overlay.
  - 1 device → 1 line (현 환경), N device → N lines (자동 비교).

- [x] **P2** pcie aer counters (per-device)
  - core/monitor.py: discovered.nvme_ctrls의 address로 PCI sysfs path 매핑.
    aer_dev_{correctable,fatal,nonfatal} 파일 존재 검사 후 self._aer_paths
  - 컬럼: {ctrl}_aer_{cor,fatal,nonfatal} (각 파일의 TOTAL_ERR_* 라인 raw 누적값)
  - 없는 시스템/디바이스는 silent skip (컬럼 미생성)
  - 검증: GB10 nvme0 → 모든 카운터 0 (정상), 컬럼 3개 정상 추가

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
