# ebpf_io/ — Block-layer I/O Profiler collector

> 위치: `monitoring/collectors/ebpf_io/`. Python orchestrator는 `collector.py`,
> C/BPF 소스 + Makefile은 `src/`. `Session`은 `EbpfIoCollector`(`__init__.py`)를
> 통해 이걸 구동한다.

eBPF 기반 full-stack I/O 지연 분석 도구. fio(또는 임의 워크로드)가 도는 동안 커널 블록 계층 + 엔진(libaio/io_uring) 경로의 각 구간 지연을 maps에 누적하고, 사용자 공간에서 JSON으로 뽑아 페이즈별 breakdown 테이블을 생성한다.

## 핵심 아이디어: Full-Stack Latency Breakdown

I/O 한 건의 전체 시간을 경계 지점으로 잘라 페이즈별로 측정한다. 이 페이즈 정의가 이 코드의 존재 이유이므로, 어디든 손대기 전에 머릿속에 박혀 있어야 한다. 페이즈 약어는 libaio·io_uring 공통이다 (X2Y = 경계 X→경계 Y 사이 구간).

```
 User                                                                       User
  │                                                                          ▲
  │ submit                                                              reap │
  ▼    S2Q        Q2D        D2CQ        CQ2C        C2R         R2U         │
  ●─────────►●─────────►●──────────►●──────────►●──────────►●────────────────┘
 submit    block_q   rq_issue   nvme_compl   rq_complete   engine
           (bio in)  (dispatch)  (= CQ)      (blk done)    complete (CQE/aio)

  S2Q  : submit            → block_bio_queue       (제출 경로)
  Q2D  : block_bio_queue   → block_rq_issue        (블록 큐 대기)
  D2CQ : block_rq_issue    → nvme_complete_rq      (device 왕복, NVMe HW)
  CQ2C : nvme_complete_rq  → block_rq_complete     (block 완료 처리, softirq)
  C2R  : block_rq_complete → aio_complete / CQE    (엔진 완료 핸드오프)
  R2U  : aio_complete      → io_getevents 반환     (user 수확, libaio 전용)
```

- 경계 **CQ** = `nvme_complete_rq` (NVMe Completion Queue 엔트리 처리 시점). **D2C = D2CQ + CQ2C** — nvme_complete_rq tracepoint가 있을 때만 분리되고, 없으면 D2C 단일 구간으로 fallback.
- **Generic mode**: Q2D, D2C만 측정 (블록 계층 tracepoints만 attach). 어떤 ioengine이든 잡힌다.
- **Libaio mode**: 위 + S2Q, C2R, R2U. `io_submit`/`io_getevents` syscall tracepoint와 `aio_complete` kprobe를 추가로 attach. submit 경계 = `sys_enter_io_submit`.
- **Iouring mode**: 위 + S2Q, C2R (R2U 없음). `io_uring_submit_req`/`io_uring_complete` tracepoint를 추가로 attach. submit 경계 = `io_uring_submit_req`. io_uring은 완료를 CQ ring으로 전달(syscall 없음)해 R2U에 해당하는 측정 지점이 없다 — S2Q/Q2D/D2C/C2R 4페이즈로 끝난다.

S2Q는 `pid_submit_start` 맵을, C2R은 `iocb_comp_start` 맵을 libaio·io_uring이 공유한다 (모드 상호배타). io_uring의 C2R 상관: block 계층에서 꺼낸 kiocb 포인터와 `io_uring_complete`의 `req` 포인터가 동일 주소다 — `io_kiocb`의 `cmd` union이 offset 0이라 `req == &io_rw->kiocb`.

## 3-layer architecture

```
io_trace.bpf.c   (kernel BPF programs)   ── attach to tracepoints/kprobes
       │                                    populate eBPF maps
       ▼
io_trace.c       (C userspace loader)    ── libbpf로 skeleton open/load/attach
       │                                    SIGUSR1 reset / 주기적으로
       ▼                                    print_json_report()로 stdout 출력
collector.py     (Python orchestrator)   ── io_trace를 Popen, 워크로드 thread 실행
                                            JSON 마커 파싱 → CSV + 최종 리포트
```

세 레이어 사이의 계약(contract)이 깨지면 silent failure가 난다. 특히 **JSON 마커**(`---JSON_START---`/`---JSON_END---`)와 **필드 키 이름**은 양쪽이 합의해야 한다.

### Layer 1: `io_trace.bpf.c` — BPF programs

Attach points:
| Program | Hook | Mode |
| --- | --- | --- |
| `trace_submit_enter/exit` | `tp/syscalls/sys_enter_io_submit`, `sys_exit_io_submit` | libaio |
| `trace_getevents_enter/exit` | `sys_enter_io_getevents`, `sys_exit_io_getevents` | libaio |
| `trace_pgetevents_enter/exit` | `sys_enter_io_pgetevents`, `sys_exit_io_pgetevents` | libaio |
| `trace_aio_complete` | `kprobe/aio_complete` | libaio |
| `io_uring_submit_req` | `tp_btf/io_uring_submit_req` | iouring |
| `io_uring_complete` | `tp_btf/io_uring_complete` | iouring |
| `block_bio_queue` | `tp_btf/block_bio_queue` | always |
| `block_rq_issue` | `tp_btf/block_rq_issue` | always |
| `block_rq_complete` | `tp_btf/block_rq_complete` | always |

`opt_trace_libaio` / `opt_trace_iouring`는 BPF rodata 변수. 사용자 공간에서 load 전에 세팅하고, 해당 모드가 아니면 C에서 `bpf_program__set_autoattach(..., false)`로 그 모드 전용 프로그램들을 disable한다. 두 모드는 상호배타적이다. C2R 경로 판별용 `addr_iomap_dio_end_io` / `addr_blkdev_end_io` / `addr_blkdev_end_io_async` rodata도 같은 시점에 `/proc/kallsyms`에서 읽은 주소로 세팅한다 (아래 "bio→iocb 매핑" 참고).

Maps (전부 `io_trace.bpf.c`의 `SEC(".maps")`에서 선언):

| Map | Type | Key | Value | 용도 |
| --- | --- | --- | --- | --- |
| `bio_start` | HASH | `bio*` | `bio_start_ctx` | bio enqueue 시각 (Q2D 시작점) |
| `req_start` | HASH | `request*` | `trace_ctx` | rq issue 시각 + 직전 Q2D 지연 |
| `device_stats` | **PERCPU_HASH** | `dev_id` (maj<<20\|min) | `io_stats` | 디바이스 단위 누적 통계 |
| `pid_submit_start` | HASH | `pid_tgid` | `u64 ts` | submit 시각 (S2Q 시작점, libaio·io_uring 공유) |
| `active_getevents_events` | HASH | `pid_tgid` | `events ptr` | io_getevents의 events 인자 |
| `iocb_complete_ts` | HASH | `{pid_tgid,iocb}` | `u64 ts` | aio_complete 시각 (R2U 시작점) |
| `iocb_comp_start` | HASH | `iocb*` | `comp_ctx` | rq_complete 시각 (C2R 시작점, libaio·io_uring 공유) |
| `engine_stats_map` | ARRAY[1] | 0 | `engine_stats` | 엔진 페이즈(S2Q/C2R/R2U) 글로벌 누적 |
| `scratch_stats` | PERCPU_ARRAY[1] | 0 | `io_stats` | 0-초기화용 임시 버퍼 |
| `dev_capacity_map` | HASH | `dev_id` | `u64 sectors` | LBA bucket 계산용 (디바이스 용량) |

핵심 데이터 구조 (`io_trace.h`):
- `io_req_type`: READ=0, READ_AHEAD=1, WRITE=2, FLUSH=3, DISCARD=4 — 이 순서는 C와 Python 양쪽이 의존한다.
- `rw_stats`: io_count, total_bytes, q2d/d2c(lat_stats), size_hist[4], lba_hist[64], **q2d_hist[32], d2c_hist[32]** (log2(ns) latency buckets — bucket b = `floor(log2(ns))`, b=0이 1~2ns, b=10이 ~1us, b=20이 ~1ms, b=30이 ~1s; LAT_HIST_BUCKETS-1로 clamp). QD는 별도 `device_qd` 맵.
- `lat_stats`: total/max/min (ns 단위).
- 디바이스 키 인코딩: `(major << 20) | minor`. unpack은 `major = key >> 20`, `minor = key & 0xFFFFF`.
- 사이즈 히스토그램 버킷: `<=4K | 4K~32K | 32K~128K | >128K` (4-bucket, `block_rq_complete`에서 분류).
- LBA 히트맵: 디바이스 용량을 64등분해서 `(sector * 64) / capacity_sectors`로 bucket index 계산.

QD 추적: `block_rq_issue`에서 `current_qd++`, `block_rq_complete`에서 `current_qd--`. PERCPU_HASH이므로 음수가 될 수 있고, 사용자 공간에서 모든 CPU 값을 합산해야 의미 있는 값이 된다. `max_qd`는 per-CPU에서 갱신 후 user-space에서 `max()` reduce.

### Layer 2: `io_trace.c` — userspace loader

핵심 흐름:
1. argv 파싱 (`-m/--mode`, `-i/--interval`).
2. `io_trace_bpf__open()` → `skel->rodata->opt_trace_libaio` + dio 완료 콜백 주소(`addr_*_end_io`, `resolve_ksym()`이 `/proc/kallsyms`에서 추출) 설정 → 모드에 따라 syscall 프로그램들 autoattach off → `__load()`.
3. `/sys/dev/block/*/size`를 읽어 `dev_capacity_map`을 채운다 (LBA 정규화에 필요).
4. `__attach()` 후 1초 sleep 루프.
5. `SIGUSR1` → `clear_stats_map(device_stats)` (Python이 워크로드 시작 직전 리셋용으로 보냄).
6. `opt_interval`초마다 `print_json_report()` 호출, 종료 시 마지막 리포트 1회 더.

`print_json_report()`는 `---JSON_START---` … `---JSON_END---` 마커 사이에 단일 JSON 객체를 출력한다. **PERCPU_HASH 합산**(nr_cpus만큼 stats_array를 받아 sum/max/min)도 여기서 수행. `total_any_io == 0`인 디바이스는 출력에서 스킵한다.

JSON 스키마 (이게 layer 사이 contract):
```json
{
  "devices": [
    {
      "dev_name": "dev(259:0)",
      "operations": {
        "read"|"write"|"read_ahead"|"flush"|"discard": {
          "total_count": <u64>, "total_bytes": <u64>,
          "current_qd": <int>, "max_qd": <u32>,
          "size_hist": [u64, u64, u64, u64],
          "lba_hist": [u32 × 64],
          "q2d": {"total_lat_ns": u64, "min_lat_ns": u64, "max_lat_ns": u64},
          "d2c": {"total_lat_ns": u64, "min_lat_ns": u64, "max_lat_ns": u64},
          "q2d_hist": [u64 × 32],  // log2(ns) latency buckets
          "d2c_hist": [u64 × 32],
          "d2c_split": {"d2cq_total_ns": u64, "cq2c_total_ns": u64, "traced_count": u64}
        }
      },
      "sqcq": {"same": u64, "diff": u64}    // device-level: SQ(issue) CPU == CQ(complete) CPU 여부 누적
    }
  ],
  "engine_overhead": {
    "s2q_count": ..., "s2q_lat_total": ...,
    "c2r_{read,write,flush}_count|total": ...,
    "r2u_{read,write,flush}_count|total": ...
  }
}
```

`engine_overhead`는 libaio·io_uring 공용 단일 블록 — 모드 상호배타라 하나의 `engine_stats` 구조체/맵을 둘이 공유한다. 비활성 페이즈는 0 (io_uring은 `r2u_*` = 0, generic은 전부 0).

### Cross-cutting: System metrics

`collector.py`는 standalone 실행 시 워크로드 thread 시작 직전에 `from monitoring.collectors.system import SystemMonitor`로 통합 시스템 메트릭 수집기를 띄운다 (`--no-sysmon`이면 생략 — `Session`이 띄울 때). eBPF I/O CSV (`{dev}_{session}.csv`)와 같은 디렉터리에 `system_metrics_{session}.csv` + `topology_{session}.json`이 함께 떨어진다. timestamp 컬럼으로 join 가능. SystemMonitor 자체는 eBPF와 무관하므로 손댈 일 있으면 `monitoring/collectors/system.py`만 보면 됨.

### Layer 3: `collector.py` — Python orchestrator

`run_benchmark(mode, cmd|script_file, interval)`:
1. `sudo ./io_trace -i {interval} [-m {mode}]`을 `Popen`(stdout=PIPE).
2. `time.sleep(1.5)`로 attach 안정화 대기 → `drop_caches` → `SIGUSR1`로 통계 리셋 (워크로드 시작 직전 상태에서 0부터).
3. 워크로드는 별도 thread(`run_workload_thread`)에서 `subprocess.run(cmd_or_bash_script_file)`. 워크로드 종료 시 메인 PID에 `SIGINT`를 쏴서 깨끗하게 정리.
4. 메인 thread는 trace_proc.stdout을 라인 단위로 읽으며 `---JSON_START---`/`---JSON_END---` 사이를 버퍼링.
5. 매 JSON마다 `parse_and_store_metrics()`로 누적값을 **delta**로 변환해 IOPS/BW/avg-lat 계산, 5초마다 `save_csv_buffers()`로 flush.
6. 종료 시 마지막 JSON으로 `print_final_summary()` — phase × {Total, READ, WRITE, READ-AHEAD, FLUSH}의 Call/Sum(ms)/Avg(us) 테이블 출력. operation별 `Q2D pct`, `D2C pct` 라인에 p50/p95/p99/p99.9 (`compute_percentiles(hist)`가 log2 bucket을 선형 보간하여 us로 변환).

CSV 출력 위치: `{output_dir}/{real_dev_name}_{SESSION_ID}.csv`. `Session`이 구동할 땐 `--output-dir`로 세션 디렉터리가 주입되고, standalone 실행 시엔 `results/ebpf_standalone/`. 디바이스 이름은 `dev(maj:min)` → `/sys/dev/block/maj:min` realpath로 `nvme0n1` 같은 실명으로 변환.

CSV 컬럼: timestamp, operation, iops_interval, bandwidth_mb_s_interval, q2d_avg_us_interval, d2c_avg_us_interval, **s2q_avg_us_interval, c2r_avg_us_interval, r2u_avg_us_interval** (S2Q/C2R은 양 엔진, R2U는 libaio에서만 0 이상 값), **sq_cq_diff_ratio** (디바이스 단위, 같은 인터벌의 모든 op row에 동일), **d2c_p50_us, d2c_p99_us, q2d_p99_us** (인터벌 히스토그램 delta에서 계산한 percentile — `prev_hists` 글로벌 dict로 추적), current_qd, max_qd, total_io_count, total_bytes, q2d/d2c {total,min,max}_ns, size_hist_{4k,32k,128k,large}, lba_0 … lba_63. s2q는 글로벌(같은 인터벌 내 모든 행 동일). c2r/r2u는 op별이며 read_ahead/discard는 완료측 경로 없어 0.

## Build / Run

```bash
# 빌드 (프로젝트 어디서든)
make -C monitoring/collectors/ebpf_io/src         # vmlinux.h → BPF obj → skeleton → io_trace
make -C monitoring/collectors/ebpf_io/src clean

# 단독 실행 (raw JSON을 stdout에 흘림)
sudo monitoring/collectors/ebpf_io/src/io_trace -m libaio -i 1

# 워크로드와 함께 실행 (Session 밖 standalone 경로)
python3 monitoring/collectors/ebpf_io/collector.py -m generic -i 1 -c "fio --name=t --filename=/dev/nvme0n1 ..."
python3 monitoring/collectors/ebpf_io/collector.py -m libaio  -i 0 -f src/fio.sh

# 보통은 pmon.py가 EbpfIoCollector를 통해 구동 (권장)
./pmon.py monitor --fio "fio ..." --ebpf on
```

옵션:
- `-m {generic|libaio|iouring}` — 모드별 추가 페이즈는 위 "Full-Stack" 절 참고. iouring은 fio `--ioengine=io_uring` 워크로드라야 S2Q/C2R이 잡힌다.
- `-i N` — N초마다 CSV 한 줄. `-i 0`이면 timeseries 비활성, 최종 summary만.
- `-c` vs `-f` — mutually exclusive. 둘 다 없으면 무한 대기(수동 조작용).

빌드 의존성: `clang`, `bpftool`, `libbpf-dev`, `libelf-dev`, `zlib1g-dev`. 커널은 BTF가 켜져 있어야 하며 (`/sys/kernel/btf/vmlinux` 존재), tp_btf 사용을 위해 5.x 이상 권장.

## Layer 간 컨벤션

수정 시 깨지기 쉬운 항목들:

- **`io_req_type` enum 순서** — `io_trace.bpf.c`의 분기, `io_trace.c`의 `type_names[]`, Python의 operation 키("read"/"read_ahead"/"write"/"flush"/"discard")가 전부 같은 순서/이름. 하나 바꾸면 셋 다 바꿔야 한다.
- **JSON 마커** — `---JSON_START---` / `---JSON_END---` 문자열. Python 파서가 라인 단위로 매칭하므로 줄을 합치거나 prefix를 추가하면 안 됨.
- **dev key 인코딩** — `(maj << 20) | min`. 다른 곳에서 보통 `MKDEV`는 `(maj << 8) | min`을 쓰는데 여기는 다르다. minor가 20bit까지 들어갈 수 있도록 의도된 선택.
- **size_hist 경계** — 4096 / 32768 / 131072 byte. Python CSV 컬럼명(`size_hist_4k`, `_32k`, `_128k`, `_large`)이 이걸 가정.
- **PERCPU 합산은 user-space 책임** — BPF 측에서 PERCPU map 값을 그대로 노출하면 CPU별 부분합만 보인다. `io_trace.c::print_json_report`의 `for (i = 0; i < nr_cpus; i++)` 루프가 그 역할.
- **`runtime`은 BPF가 모름** — fio runtime / Python `effective_duration` / interval-기반 delta는 각자 다른 시간 기준이다. 최종 리포트의 BW(MB/s)는 `total_bytes / effective_duration`을 쓰고, CSV의 `bandwidth_mb_s_interval`은 interval 사이 delta를 쓴다.
- **bio→iocb 매핑은 bi_end_io 주소로 경로 판별** — `block_rq_complete`에서 `kiocb`를 꺼낼 때, `bio->bi_end_io` 주소를 rodata로 주입된 세 dio 완료 콜백과 비교해 I/O 경로를 가른다. `iomap_dio_bio_end_io`(파일 direct I/O)·`blkdev_bio_end_io`(raw blockdev 멀티-bio)는 `bi_private`가 dio를 가리키고, `blkdev_bio_end_io_async`(raw blockdev 단일-bio)는 `bi_private`를 안 채워 bio가 내장된 `blkdev_dio`를 `container_of`로 역산한다. `kiocb`는 `iomap_dio`·`blkdev_dio` 모두 offset 0이라 dio 포인터만 구하면 추출은 동일. 세 콜백 중 어느 것도 아니면(분할 bio = `bio_chain_endio`, page-cache writeback 등) C2R은 best-effort로 skip. 새 I/O 경로 추가 시 그 경로의 end_io를 판별 분기에 더해야 한다.

## 알려진 sharp edges / 작업 후보

- **C2R는 kallsyms 의존** — C2R 상관관계는 `bio->bi_end_io`를 `/proc/kallsyms`에서 읽은 세 dio 완료 콜백 주소와 비교한다 (libaio·io_uring 공통). 파일 direct I/O(ext4 등, iomap 경로)와 raw block device direct I/O(`blkdev_dio` 경로) **모두** C2R/R2U가 잡힌다. 단 `CONFIG_KALLSYMS`가 꺼져 있거나 심볼 이름이 다른 커널에선 해당 경로의 C2R만 누락된다 (`io_trace`가 stderr에 warn 출력, S2Q/Q2D/D2C는 영향 없음). 분할 bio(`bio_chain_endio`)는 best-effort skip. smoke(`.smoke/smoke.img`, ext4 파일)는 iomap 경로를 커버하고, raw 경로는 `io_trace -m libaio -c "fio --filename=/dev/<dev> ..."`로 검증 가능.
- **iouring R2U 대응 없음** — io_uring은 CQE를 CQ ring에 게시하고 사용자는 syscall 없이 ring을 읽는다. libaio의 R2U(엔진 완료→사용자 수확)에 해당하는 측정 지점이 없어 의도적으로 4페이즈(S2Q/Q2D/D2C/C2R)에서 멈춘다.
- **SQPOLL** — SQPOLL 모드면 `io_uring_submit_req`가 poller kthread에서 실행되지만 `block_bio_queue`도 같은 kthread라 S2Q의 pid_tgid 키 상관은 유지된다.
- **PERCPU_HASH max_entries=256** — 디바이스 수 상한. 일반 시스템에선 충분하지만 멀티-경로/멀티-디스크 환경에서 한계 가능.
- **루프 unroll `#pragma unroll for (i=0; i<256; i++)`** — `io_getevents` 결과 256개까지만 처리. nr > 256인 거대한 batch는 일부 누락.
- ~~**`opt_interval`이 1초 미만이 안 됨**~~ — `io_trace.c` 메인 루프가 이제 `nanosleep` + float `opt_interval` 사용. `-i 0.5` 등 sub-second 가능 (최소 50ms로 clamp). `collector.py`의 `-i` 도 float. **단** SystemMonitor의 nvidia-smi dmon은 1초 미만 인터벌 지원 안 함 → `int(max(1, interval))`로 clamp되어 GPU 메트릭만 1초 주기 유지.
- **CSV는 append 모드** — 같은 디렉터리에서 재실행하면 `SESSION_ID`가 달라져 새 파일이 생기지만, 디바이스 이름이 충돌하면 같은 파일에 이어붙는다. 의도된 동작인지 검토.
- **`sample.txt`, `io_trace.bpf.o`, `io_trace.skel.h`, `io_trace`(바이너리)** — 빌드/실험 산출물. `.gitignore`에 `monitoring/collectors/ebpf_io/src/io_trace`, `*.o`는 들어 있지만 skel.h, sample.txt는 추적 중. 새 워크플로 추가 시 정리 여부 결정.
- **`vmlinux.h`가 4MB 가까이 됨** — 시스템 커널 BTF 덤프. 다른 커널/머신에서 빌드하려면 `make vmlinux.h`로 재생성 필요.
- **fio.sh의 워크로드** — 현재 Seq Write/Read 1M만 활성, Random 4K는 주석 처리. 테스트 시나리오 바꿀 일 잦으니 인자화 고려.

## 작업 시 출발점 매핑

| 하고 싶은 일 | 손대야 할 곳 |
| --- | --- |
| 새 페이즈/지연 추가 (예: scheduler 큐 진입) | `io_trace.h`의 struct → `io_trace.bpf.c`에 hook 추가 → maps에 누적 → `print_json_report`에 필드 추가 → Python `phase_stats`/CSV 컬럼 추가 |
| 새 ioengine 지원 (iouring 등) | `io_trace.bpf.c`에 해당 syscall tracepoint 추가 → `opt_trace_*` rodata 플래그 패턴 따라가기 → `io_trace.c`의 mode 분기에 autoattach 토글 추가 → `collector.py`의 `choices`와 모드 분기 |
| 출력 포맷 추가 (예: Prometheus, parquet) | `collector.py::parse_and_store_metrics`/`print_final_summary`만 건드리면 됨 — JSON 컨트랙트는 유지 |
| 새 메트릭 (예: p99 latency) | BPF 단에서 히스토그램 추가 (현재는 mean/min/max만 있음). `lat_stats`에 bucket array 추가하는 게 표준 패턴 |
| LBA 해상도 변경 | `io_trace.h::LBA_BUCKETS` → `collector.py`의 `lba_{i}` 컬럼 생성 루프 및 `+ [f"lba_{i}" for i in range(64)]` 동기화 |
