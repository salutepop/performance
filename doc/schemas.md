# 산출물 스키마 명세

이 문서는 perf 모니터링 프레임워크가 만드는 모든 파일의 컬럼/필드 정의를 모은다. 분석 스크립트, 대시보드, 회귀 자동화를 작성할 때 참고용. **스키마는 add-only**(컬럼/필드 추가만 허용, 의미 변경/제거 금지) — 외부 도구 호환성 유지.

## 1. `<device>_<session_id>.csv` (device per-second I/O CSV)

`io_profiler.py`가 1초 단위(또는 `-i` 옵션 값)로 디바이스별 op 행을 추가. 한 행 = 한 (timestamp, operation) 조합.

| 컬럼 | 단위/타입 | 의미 |
|---|---|---|
| `timestamp` | HH:MM:SS | 인터벌 종료 시각 (로컬 wall time) |
| `operation` | str | `read` / `write` / `read_ahead` / `flush` / `discard` |
| `iops_interval` | int | 인터벌 내 I/O 완료 건수 |
| `bandwidth_mb_s_interval` | float MB/s | 인터벌 평균 대역폭 |
| `q2d_avg_us_interval` | float us | 인터벌 평균 Q2D (블록큐 진입→dispatch) |
| `d2c_avg_us_interval` | float us | 인터벌 평균 D2C (dispatch→completion, 디스크 실제 처리 시간) |
| `u2q_avg_us_interval` | float us | libaio 모드 한정: 인터벌 평균 U2Q (syscall→block_bio_queue). 글로벌 값이라 같은 인터벌 모든 op 행에 같은 값 들어감 |
| `c2a_avg_us_interval` | float us | libaio 모드 한정: 인터벌 평균 C2A (rq_complete→aio_complete). op별 |
| `a2u_avg_us_interval` | float us | libaio 모드 한정: 인터벌 평균 A2U (aio_complete→user wakeup). op별 |
| `sq_cq_diff_ratio` | float [0,1] | SQ(issue) CPU vs CQ(completion) CPU 불일치 비율. **device 단위** — 같은 인터벌 모든 op 행에 같은 값 |
| `d2c_p50_us` | float us | 인터벌 d2c log2(ns) 히스토그램 delta 기준 p50 |
| `d2c_p99_us` | float us | 인터벌 d2c p99 (tail latency) |
| `q2d_p99_us` | float us | 인터벌 q2d p99 (블록큐 정체) |
| `current_qd` | int | snapshot 시점의 inflight QD (글로벌 device_qd map) |
| `max_qd` | int | 세션 시작부터 누적 max QD |
| `total_io_count` | u64 | 누적 I/O 완료 수 |
| `total_bytes` | u64 | 누적 바이트 |
| `q2d_total_ns` / `q2d_min_ns` / `q2d_max_ns` | u64 | 누적 Q2D 통계 |
| `d2c_total_ns` / `d2c_min_ns` / `d2c_max_ns` | u64 | 누적 D2C 통계 |
| `size_hist_4k` / `size_hist_32k` / `size_hist_128k` / `size_hist_large` | u64 | 누적 size 히스토그램. 경계는 `<=4096`, `<=32768`, `<=131072`, `>131072` bytes |
| `lba_0` … `lba_63` | u32 | 누적 LBA bucket 접근 횟수. bucket b는 `(sector * 64) / capacity_sectors` |

예시 (첫 두 행):
```csv
timestamp,operation,iops_interval,bandwidth_mb_s_interval,q2d_avg_us_interval,d2c_avg_us_interval,u2q_avg_us_interval,c2a_avg_us_interval,a2u_avg_us_interval,sq_cq_diff_ratio,d2c_p50_us,d2c_p99_us,q2d_p99_us,current_qd,max_qd,...
00:21:48,read,47599,185.93,4.12,93.58,2.34,0.75,206.13,0.0001,82.0,256.0,8.0,8,28,...
```

**Phase 정의**: U2Q · Q2D · D2C · C2A · A2U → I/O 한 건 전체 latency를 5단계로 나눈 ebpf/CLAUDE.md "Full-Stack Latency Breakdown" 참고.

## 2. `system_metrics_<session_id>.csv` (system metrics CSV)

`SystemMonitor`가 1초마다 1행씩 append. 컬럼은 시스템 토폴로지에 따라 동적으로 결정됨 (NUMA 노드 수, NVMe 컨트롤러 수, GPU 수에 비례해서 늘어남).

| 컬럼 패턴 | 단위 | 의미 |
|---|---|---|
| `timestamp` | HH:MM:SS | 인터벌 종료 시각 |
| `node{N}_user_pct` / `_sys_pct` / `_iowait_pct` / `_irq_pct` / `_softirq_pct` | % | NUMA 노드 N의 `/proc/stat` 인터벌 사용률 |
| `node{N}_freq_avg_mhz` / `_freq_max_mhz` | MHz | NUMA 노드 N의 CPU 주파수 평균/최대. ARM AMU 즉시값은 일시 spike (>nominal max) 가능 — raw 보존 |
| `node{N}_mem_free_mb` / `_mem_used_mb` | MB | NUMA 노드 N 메모리 (/sys/.../node{N}/meminfo). 다중 노드 시스템에서만 생성 |
| `{ctrl}_irq_per_s` | int/s | NVMe 컨트롤러 IRQ rate (/proc/interrupts의 `nvme<X>q<Y>` 라인 인터벌 delta) |
| `{ctrl}_top_cpu` | int | 인터벌 중 IRQ 가장 많이 받은 CPU id |
| `{ctrl}_top_cpu_node` | str | 그 CPU의 NUMA 노드 |
| `{ctrl}_aer_cor` / `_aer_fatal` / `_aer_nonfatal` | u64 | PCIe AER counter raw 누적값 (`/sys/bus/pci/devices/<addr>/aer_dev_*`의 TOTAL_ERR_*). 정상은 0 유지 |
| `mem_available_mb` / `mem_dirty_mb` / `mem_writeback_mb` / `swap_used_mb` | MB | 글로벌 `/proc/meminfo` |
| `pgpgin_per_s` / `pgpgout_per_s` | int/s | `/proc/vmstat` block I/O 인터벌 delta |
| `pswpin_per_s` / `pswpout_per_s` | int/s | swap activity (정상 워크로드 0) |
| `loadavg_1m` | float | `/proc/loadavg` 첫 값 |
| `gpu{N}_pwr_w` / `_temp_c` / `_sm_pct` / `_mem_pct` / `_mem_used_mb` / `_pcie_rx_mb_s` / `_pcie_tx_mb_s` | mixed | `nvidia-smi dmon -s pumt` 스트림. 없는 metric은 빈 칸 (예: GB10 unified memory의 fb/pcie) |

## 3. `topology_<session_id>.json`

세션 시작 시 1회 dump. cpu↔NUMA, NVMe 컨트롤러, GPU 정적 정보.

```json
{
  "session_id": "20260519_005046",
  "nodes": ["0"],
  "cpu_to_node": {"0": "0", "1": "0", ...},
  "nvme_controllers": ["nvme0", ...],
  "gpus": [
    {"index": 0, "name": "...", "pci_bus_id": "...", "numa_node": "..."}
  ],
  "raw": {
    "cpu": {"total_cores": 20, "model": "...", "threads_per_core": 1},
    "numa": {"0": {"cpus": "0-19"}, ...},
    "storage": [
      {"name": "/dev/nvme0n1", "ctrl": "nvme0", "numa_node": "...", "path": "..."}
    ],
    "nvme_ctrls": [
      {"name": "nvme0", "model": "...", "state": "live", "firmware_rev": "...",
       "serial": "...", "transport": "pcie", "address": "0004:01:00.0",
       "cntrltype": "io", "queue_count": 16, "numa_node": "...", "subsysnqn": "..."}
    ],
    "memory": {"total_gb": 125.5, "details": [...]},
    "gpu": [{"index": 0, "name": "...", "pci_bus_id": "...", "numa_node": "..."}]
  }
}
```

**주의**: `raw`의 하위는 `discovered.X`가 아니라 **flat** (예: `raw.nvme_ctrls`, `raw.gpu`). 일부 SystemMonitor 코드는 `sys_info.get("discovered", sys_info).get(...)` 패턴으로 양쪽 지원.

## 4. `summary_<session_id>.json` (평탄 JSON export)

`report/summary.py` 산출. 외부 도구가 소비하기 쉽게 평탄화.

```json
{
  "session_id": "20260519_002145",
  "generated_at": "2026-05-19T00:30:00",
  "source_dir": "/abs/path",
  "topology": {"nodes": [...], "nvme_controllers": [...], "gpus": [...]},
  "devices": {
    "nvme0n1": {
      "sqcq_diff_ratio": {"avg": 0.11, "max": 0.33},
      "ops": {
        "read":  {"total_io": 513090, "bw_mb_avg": 334.0, "bw_mb_peak": 402.7,
                   "q2d_us_avg": 4.07, "d2c_us_avg": 94.27, "qd_peak": 64},
        "write": {...}
      }
    }
  },
  "system": {
    "cpu":      {"node0": {"user_pct_avg": 2.96, "sys_pct_avg": 13.17,
                            "iowait_pct_avg": 0.19, "iowait_pct_peak": 0.40}},
    "memory":   {"mem_available_mb_min": 107037, "mem_dirty_mb_peak": 8.0,
                  "mem_writeback_mb_peak": 0.0, "pgpgin_per_s_peak": 431610,
                  "pgpgout_per_s_peak": 418890, "loadavg_1m_peak": 1.65},
    "nvme_irq": {"nvme0": {"per_s_avg": 23096, "per_s_peak": 28060}},
    "gpu":      {"gpu0": {"sm_pct_peak": 4, "power_w_peak": 7,
                           "temp_c_peak": 43, "mem_used_mb_peak": null}}
  }
}
```

## 5. HTML 리포트 상단 카드 (Executive Summary)

`report_*.html` 최상단 8장의 카드는 `report/html_report.py::_render_summary` 가 만든다. 모든 카드는 `report/md_report.py::_device_aggregates` (device CSV → op별 stats) + `_system_aggregates` (system CSV → node/iface별 stats) 두 dict에서 추출.

### 카드 매핑 표

| # | 카드 label | 집계 방법 | 입력 CSV 컬럼 | CSV 생성 위치 | 원본 데이터 출처 | 경고 임계 |
|---|---|---|---|---|---|---|
| 1 | **Read IOPS (total)** | Σ devices × op∈{read, read_ahead} : `iops.sum` | `iops_interval` | `ebpf/io_profiler.py::parse_and_store_metrics` | BPF `block_rq_complete` → `device_stats[dev].stats[IO_READ].io_count` 인터벌 delta | — |
| 2 | **Write IOPS (total)** | Σ devices : `iops.sum` (op=write) | `iops_interval` (op=write 행) | 위와 동일 | 위와 동일 (`type=IO_WRITE`) | — |
| 3 | **Peak BW** | max(devices × ops : `bw.max`) | `bandwidth_mb_s_interval` | 위와 동일 | BPF `target->total_bytes` 인터벌 delta ÷ elapsed | — |
| 4 | **Worst D2C interval avg** | max(devices × ops : `d2c.max`) | `d2c_avg_us_interval` | 위와 동일 | BPF `block_rq_issue→block_rq_complete` 시간차 (`target->d2c.total / count`) per interval | `>500us` → warn |
| 5 | **SQ↔CQ diff** | mean(devices : `_sqcq_diff_ratio.avg`) | `sq_cq_diff_ratio` | 위와 동일 | BPF `block_rq_complete`에서 `bpf_get_smp_processor_id()` vs `trace_ctx.issue_cpu` 비교 → `device_qd.sq_cq_{same,diff}` 누적의 인터벌 delta 비율 | `>20%` bad, `>5%` warn |
| 6 | **CPU iowait peak** | max(nodes : `iowait_pct.max`) | `node{N}_iowait_pct` | `core/monitor.py::_tick` | `/proc/stat` 의 per-CPU iowait jiffies 인터벌 delta → NUMA node 단위 합산 / total × 100 | `>10%` bad, `>2%` warn |
| 7 | **CPU sys peak** | max(nodes : `sys_pct.max`) | `node{N}_sys_pct` | 위와 동일 | `/proc/stat` sys jiffies | `>50%` warn |
| 8 | **GPU power peak** *(있을 때만)* | max(gpus : `pwr_w.max`) | `gpu{N}_pwr_w` | `core/monitor.py::_gpu_reader_loop` (background thread) | `nvidia-smi dmon -s pumt` stream의 `pwr` 컬럼 | `>50W` warn |

### "max"의 정확한 의미

`_stats(values)` 헬퍼가 인터벌 값 리스트(예: 7개의 1초 평균)에서 `{n, avg, min, max, sum}` 을 계산. 즉 카드 #3·#4의 "max"는 **인터벌별 평균 중 가장 큰 인터벌** — 전체 누적 평균이 아닌 **가장 격렬했던 1초**.

예) 카드 #4 "Worst D2C interval avg" 값이 7700us = "어떤 1초 동안 d2c 평균이 7.7ms였다". 통상 첫 인터벌(preconditioning) 또는 GC 트리거 시 발생.

### 호출 흐름

```
build_report(session_dir, sid)
  ├─ _load_csv(system_metrics_<sid>.csv)            ← row 단위 system_metrics
  ├─ _system_aggregates(sys_h, sys_r)               ← cpu/mem/irq/gpu/freq stats dict
  ├─ for dpath in *_<sid>.csv:
  │     _load_csv(dpath)                            ← row 단위 device CSV
  │     _device_aggregates(dh, dr)                  ← op별 {iops,bw,q2d,d2c,qd}.stats dict
  └─ _render_summary(dev_aggs, sys_agg)
        ├─ 위 8개 값 추출 + 임계값 비교로 card class 결정 (`warn` / `bad` CSS class)
        ├─ html.escape + <div class='card …'> 생성
        └─ _top_findings(sys_agg, dev_aggs) 호출 → <ul class='findings'> 렌더
```

### 한 데이터 흐름으로 본 예시 — 카드 #4

```
fio randread → 커널 block layer
  ↓ tp/block_rq_issue: trace_ctx에 issue_ts 저장
  ↓ tp/block_rq_complete: d2c_lat = now - issue_ts → target->d2c.total += d2c_lat (BPF map device_stats)
  ↓ io_trace.c (userspace loader): 1초마다 device_stats dump → JSON에 d2c.{total,min,max}
  ↓ io_profiler.py: prev/curr 누적 delta → d2c_avg_us_interval = (Δtotal / Δcount / 1000)
  ↓ CSV row append: nvme0n1_<sid>.csv 의 d2c_avg_us_interval 컬럼
  ↓ md_report._device_aggregates: 인터벌 값 리스트 → _stats → {"max": 가장 큰 인터벌 평균}
  ↓ html_report._render_summary: dev_aggs[d][op]["d2c"]["max"] → 카드 #4 표시
```

### Top findings (카드 아래)

`md_report._top_findings(sys_agg, dev_aggs)` 의 heuristic 결과 (markdown 리포트와 동일):
- SQ↔CQ diff_ratio 평균 → cross-CPU completion 경고 또는 OK
- iowait peak → I/O wait 경고
- mem_dirty peak > 100MB → write-back 활용
- GPU SM peak > 10% → GPU 활동
- 가장 바쁜 NUMA node 식별

### 임계값 변경 시

`report/html_report.py::_render_summary` 안의 inline 조건문. 예:
```python
("Worst D2C interval avg", _fmt_num(peak_d2c_us, " us", 1),
 "warn" if peak_d2c_us > 500 else "", ...)
```
threshold 바꾸면 같은 함수만 수정. CSS class `warn` / `bad` 색 정의는 `HTML_CSS` 변수.

## 6. 리포트 파일 (`report_*.html`, `report_*.md`, `diff_*.md`)

- `report_<sid>.html`: 자기완결 HTML. 외부 리소스는 Chart.js CDN(`jsdelivr.net`) 1개만 사용, 미접속 환경에서는 차트 자리에 "Chart.js CDN unreachable" 표시되고 테이블/SVG는 정상 렌더.
- `report_<sid>.md`: Top findings + Topology + Device aggregate + System aggregate 요약. 평균/peak 단순 산술평균 (인터벌 outlier에 민감 — P3 follow-up).
- `diff_<base>_vs_<cand>.md`: 두 세션 비교. Marker: `⚠` `|Δ|≥5%`, `⛔` `|Δ|≥20%`.
