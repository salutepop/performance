#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "io_trace.h"

char LICENSE[] SEC("license") = "GPL";
#define BPF_REQ_RAHEAD (1ULL << 19)

/* log2(ns) bucket index. ns==0이면 0, 그 외는 floor(log2(ns)). clamp [0, LAT_HIST_BUCKETS-1].
 * BPF target에 __builtin_clzll 미구현 → 수동 shift 루프 (bounded, verifier-friendly). */
static __always_inline u32 lat_bucket(u64 ns) {
    if (ns == 0) return 0;
    u32 b = 0;
    #pragma unroll
    for (u32 i = 1; i < LAT_HIST_BUCKETS; i++) {
        if (ns >> i) b = i;
    }
    return b;
}

const volatile bool opt_trace_libaio = false;
const volatile bool opt_trace_iouring = false;

struct bio_start_ctx {
    u64 ts;
    u64 pid_tgid;
};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); 
    __type(value, struct bio_start_ctx); 
} bio_start SEC(".maps");

struct trace_ctx {
    u64 issue_ts;
    u64 q2d_lat;
    u64 pid_tgid;
    u32 issue_cpu;
    u64 cq_ts;  // nvme_complete_rq(=CQ 경계) tracepoint 시각 (D2C 세분화)
};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64);
    __type(value, struct trace_ctx);
} req_start SEC(".maps");

/*
 * SQ(issue) CPU x CQ(complete) CPU 카운트 매트릭스. key = (issue<<16)|cq.
 * sparse HASH — 실제로 발생한 (issue,cq) 쌍만 저장. 384코어면 최대 384*384개.
 */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 262144);
    __type(key, u32);
    __type(value, u64);
} cpu_matrix SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __uint(max_entries, 256);
    __type(key, u32); 
    __type(value, struct io_stats);
} device_stats SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10240);
    __type(key, u64); 
    __type(value, u64); 
} pid_submit_start SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10240);
    __type(key, u64); 
    __type(value, u64); 
} active_getevents_events SEC(".maps");

struct a2u_key {
    u64 pid_tgid;
    u64 iocb_ptr;
};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, struct a2u_key); 
    __type(value, u64); 
} iocb_complete_ts SEC(".maps");

/* C2R 시작점(block_rq_complete 시각 + op type). libaio·io_uring 공용. */
struct comp_ctx {
    u64 ts;
    int type;
    u64 pid_tgid;
};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64);
    __type(value, struct comp_ctx);
} iocb_comp_start SEC(".maps");

/* 엔진 페이즈(S2Q/C2R/R2U) 글로벌 누적. libaio·io_uring 공용 (mode 상호배타). */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct engine_stats);
} engine_stats_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct io_stats);
} scratch_stats SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, u32);
    __type(value, u64);
} dev_capacity_map SEC(".maps");

/*
 * QD 글로벌 카운터: cross-CPU atomic이 필요해 PERCPU가 아닌 일반 HASH를 사용.
 * key=dev_id, value=struct dev_qd ({current_qd[5], max_qd[5]}).
 */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, u32);
    __type(value, struct dev_qd);
} device_qd SEC(".maps");

/* dev_qd가 qd_hist[64] 때문에 ~570B — BPF 스택(512B)에 못 올린다.
 * zero-init용 PERCPU scratch 맵에서 memset 후 복사. */
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct dev_qd);
} scratch_qd SEC(".maps");

static __always_inline struct dev_qd *get_or_init_dev_qd(u32 dev) {
    struct dev_qd *q = bpf_map_lookup_elem(&device_qd, &dev);
    if (q) return q;
    u32 z = 0;
    struct dev_qd *init = bpf_map_lookup_elem(&scratch_qd, &z);
    if (!init) return NULL;
    __builtin_memset(init, 0, sizeof(*init));
    bpf_map_update_elem(&device_qd, &dev, init, BPF_NOEXIST);
    return bpf_map_lookup_elem(&device_qd, &dev);
}

// Libaio Tracepoints 생략 (이전 코드와 동일, 분량관계상 주요 함수만 배치)
SEC("tracepoint/syscalls/sys_enter_io_submit")
int trace_submit_enter(void *ctx) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    bpf_map_update_elem(&pid_submit_start, &pid_tgid, &ts, BPF_ANY);
    return 0;
}

SEC("tracepoint/syscalls/sys_exit_io_submit")
int trace_submit_exit(void *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    bpf_map_delete_elem(&pid_submit_start, &pid_tgid);
    return 0;
}

SEC("kprobe/aio_complete")
int trace_aio_complete(struct pt_regs *ctx) {
    struct aio_kiocb *aio_iocb = (struct aio_kiocb *)PT_REGS_PARM1(ctx);
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = 0;

    u64 key_iocb = (u64)aio_iocb;
    struct comp_ctx *cctx = bpf_map_lookup_elem(&iocb_comp_start, &key_iocb);
    if (cctx && cctx->ts > 0) {
        pid_tgid = cctx->pid_tgid;
        if (ts > cctx->ts) {
            u64 c2r_lat = ts - cctx->ts;
            u32 stat_key = 0;
            struct engine_stats *st = bpf_map_lookup_elem(&engine_stats_map, &stat_key);
            if (st) {
                if (cctx->type == IO_READ || cctx->type == IO_READ_AHEAD) {
                    __sync_fetch_and_add(&st->c2r_read_count, 1);
                    __sync_fetch_and_add(&st->c2r_read_total, c2r_lat);
                } else if (cctx->type == IO_WRITE) {
                    __sync_fetch_and_add(&st->c2r_write_count, 1);
                    __sync_fetch_and_add(&st->c2r_write_total, c2r_lat);
                } else if (cctx->type == IO_FLUSH) {
                    __sync_fetch_and_add(&st->c2r_flush_count, 1);
                    __sync_fetch_and_add(&st->c2r_flush_total, c2r_lat);
                }
            }
        }
        bpf_map_delete_elem(&iocb_comp_start, &key_iocb);
    }

    u64 key_user = BPF_CORE_READ(aio_iocb, ki_res.obj);
    if (key_user && pid_tgid) {
        struct a2u_key akey = { .pid_tgid = pid_tgid, .iocb_ptr = key_user };
        bpf_map_update_elem(&iocb_complete_ts, &akey, &ts, BPF_ANY);
    }
    return 0;
}

SEC("tracepoint/syscalls/sys_enter_io_getevents")
int trace_getevents_enter(struct trace_event_raw_sys_enter *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 events_ptr = ctx->args[3]; 
    bpf_map_update_elem(&active_getevents_events, &pid_tgid, &events_ptr, BPF_ANY);
    return 0;
}

SEC("tracepoint/syscalls/sys_exit_io_getevents")
int trace_getevents_exit(struct trace_event_raw_sys_exit *ctx) {
    long ret = ctx->ret;
    if (ret <= 0) return 0; 
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 *events_ptr_p = bpf_map_lookup_elem(&active_getevents_events, &pid_tgid);
    if (!events_ptr_p) return 0;
    u64 events_ptr = *events_ptr_p;
    bpf_map_delete_elem(&active_getevents_events, &pid_tgid);
    u32 key = 0;
    struct engine_stats *st = bpf_map_lookup_elem(&engine_stats_map, &key);
    if (!st) return 0;
    struct io_event ev;
    #pragma unroll
    for (int i = 0; i < 256; i++) {
        if (i >= ret) break; 
        if (bpf_probe_read_user(&ev, sizeof(ev), (void *)(events_ptr + i * sizeof(ev))) == 0) {
            u64 iocb_ptr = ev.obj; 
            struct a2u_key akey = { .pid_tgid = pid_tgid, .iocb_ptr = iocb_ptr };
            u64 *last_ts = bpf_map_lookup_elem(&iocb_complete_ts, &akey);
            if (last_ts && *last_ts > 0 && ts > *last_ts) {
                u64 wakeup_lat = ts - *last_ts;
                u16 opcode = 0;
                bpf_probe_read_user(&opcode, sizeof(opcode), (void *)(iocb_ptr + 16));
                if (opcode == 0 || opcode == 7) {
                    __sync_fetch_and_add(&st->r2u_read_count, 1);
                    __sync_fetch_and_add(&st->r2u_read_total, wakeup_lat);
                } else if (opcode == 1 || opcode == 8) {
                    __sync_fetch_and_add(&st->r2u_write_count, 1);
                    __sync_fetch_and_add(&st->r2u_write_total, wakeup_lat);
                } else if (opcode == 2 || opcode == 3) {
                    __sync_fetch_and_add(&st->r2u_flush_count, 1);
                    __sync_fetch_and_add(&st->r2u_flush_total, wakeup_lat);
                }
            }
            bpf_map_delete_elem(&iocb_complete_ts, &akey);
        }
    }
    return 0;
}

SEC("tracepoint/syscalls/sys_enter_io_pgetevents")
int trace_pgetevents_enter(struct trace_event_raw_sys_enter *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 events_ptr = ctx->args[3]; 
    bpf_map_update_elem(&active_getevents_events, &pid_tgid, &events_ptr, BPF_ANY);
    return 0;
}

SEC("tracepoint/syscalls/sys_exit_io_pgetevents")
int trace_pgetevents_exit(struct trace_event_raw_sys_exit *ctx) {
    long ret = ctx->ret;
    if (ret <= 0) return 0;
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 *events_ptr_p = bpf_map_lookup_elem(&active_getevents_events, &pid_tgid);
    if (!events_ptr_p) return 0;
    u64 events_ptr = *events_ptr_p;
    bpf_map_delete_elem(&active_getevents_events, &pid_tgid);
    u32 key = 0;
    struct engine_stats *st = bpf_map_lookup_elem(&engine_stats_map, &key);
    if (!st) return 0;
    struct io_event ev;
    #pragma unroll
    for (int i = 0; i < 256; i++) {
        if (i >= ret) break;
        if (bpf_probe_read_user(&ev, sizeof(ev), (void *)(events_ptr + i * sizeof(ev))) == 0) {
            u64 iocb_ptr = ev.obj;
            struct a2u_key akey = { .pid_tgid = pid_tgid, .iocb_ptr = iocb_ptr };
            u64 *last_ts = bpf_map_lookup_elem(&iocb_complete_ts, &akey);
            if (last_ts && *last_ts > 0 && ts > *last_ts) {
                u64 wakeup_lat = ts - *last_ts;
                u16 opcode = 0;
                bpf_probe_read_user(&opcode, sizeof(opcode), (void *)(iocb_ptr + 16));
                if (opcode == 0 || opcode == 7) {
                    __sync_fetch_and_add(&st->r2u_read_count, 1);
                    __sync_fetch_and_add(&st->r2u_read_total, wakeup_lat);
                } else if (opcode == 1 || opcode == 8) {
                    __sync_fetch_and_add(&st->r2u_write_count, 1);
                    __sync_fetch_and_add(&st->r2u_write_total, wakeup_lat);
                } else if (opcode == 2 || opcode == 3) {
                    __sync_fetch_and_add(&st->r2u_flush_count, 1);
                    __sync_fetch_and_add(&st->r2u_flush_total, wakeup_lat);
                }
            }
            bpf_map_delete_elem(&iocb_complete_ts, &akey);
        }
    }
    return 0;
}

/*
 * io_uring tracepoints (iouring mode). libaio의 io_submit/aio_complete에 대응.
 *   io_uring_submit_req : SQE 제출 시각 -> S2Q 시작점 (pid_submit_start 공유)
 *   io_uring_complete   : CQE 게시 시각 -> C2R 종료점
 * mode가 상호배타적이라 pid_submit_start / iocb_comp_start / engine_stats_map을
 * libaio와 공유한다. req(io_kiocb*)는 cmd union이 offset 0이라 block 계층에서
 * 꺼낸 kiocb*와 동일 주소다.
 */
SEC("tp_btf/io_uring_submit_req")
int BPF_PROG(io_uring_submit_req, void *req) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    bpf_map_update_elem(&pid_submit_start, &pid_tgid, &ts, BPF_ANY);
    return 0;
}

SEC("tp_btf/io_uring_complete")
int BPF_PROG(io_uring_complete, void *uring_ctx, void *req) {
    u64 ts = bpf_ktime_get_ns();
    u64 key = (u64)req;  // io_kiocb* == kiocb* (cmd union이 offset 0)
    struct comp_ctx *cctx = bpf_map_lookup_elem(&iocb_comp_start, &key);
    if (!cctx) return 0;
    if (cctx->ts > 0 && ts > cctx->ts) {
        u64 c2r_lat = ts - cctx->ts;
        u32 stat_key = 0;
        struct engine_stats *st = bpf_map_lookup_elem(&engine_stats_map, &stat_key);
        if (st) {
            if (cctx->type == IO_READ || cctx->type == IO_READ_AHEAD) {
                __sync_fetch_and_add(&st->c2r_read_count, 1);
                __sync_fetch_and_add(&st->c2r_read_total, c2r_lat);
            } else if (cctx->type == IO_WRITE) {
                __sync_fetch_and_add(&st->c2r_write_count, 1);
                __sync_fetch_and_add(&st->c2r_write_total, c2r_lat);
            } else if (cctx->type == IO_FLUSH) {
                __sync_fetch_and_add(&st->c2r_flush_count, 1);
                __sync_fetch_and_add(&st->c2r_flush_total, c2r_lat);
            }
        }
    }
    bpf_map_delete_elem(&iocb_comp_start, &key);
    return 0;
}

SEC("tp_btf/block_bio_queue")
int BPF_PROG(block_bio_queue, struct bio *bio) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    /* S2Q: 직전 submit(io_submit syscall / io_uring_submit_req) -> 이 시점.
     * 두 엔진이 pid_submit_start 맵을 공유하고 결과도 같은 s2q 카운터에 누적. */
    if (opt_trace_libaio || opt_trace_iouring) {
        u64 *submit_ts = bpf_map_lookup_elem(&pid_submit_start, &pid_tgid);
        if (submit_ts && ts > *submit_ts) {
            u64 s2q_lat = ts - *submit_ts;
            u32 key = 0;
            struct engine_stats *st = bpf_map_lookup_elem(&engine_stats_map, &key);
            if (st) {
                __sync_fetch_and_add(&st->s2q_count, 1);
                __sync_fetch_and_add(&st->s2q_lat_total, s2q_lat);
            }
        }
    }
    u64 bio_ptr = (u64)bio;
    struct bio_start_ctx bctx = { .ts = ts, .pid_tgid = pid_tgid };
    bpf_map_update_elem(&bio_start, &bio_ptr, &bctx, BPF_ANY);
    return 0;
}

SEC("tp_btf/block_rq_issue")
int BPF_PROG(block_rq_issue, struct request *rq) {
    u64 ts = bpf_ktime_get_ns();
    u64 req_ptr = (u64)rq;
    struct bio *bio = BPF_CORE_READ(rq, bio);
    u64 bio_ptr = (u64)bio;
    
    u64 q2d_lat = 0;
    u64 pid_tgid = 0;
    if (bio_ptr != 0) {
        struct bio_start_ctx *bctx = bpf_map_lookup_elem(&bio_start, &bio_ptr);
        if (bctx) {
            q2d_lat = ts - bctx->ts;
            pid_tgid = bctx->pid_tgid;
            bpf_map_delete_elem(&bio_start, &bio_ptr);
        }
    }

    u32 issue_cpu = bpf_get_smp_processor_id();
    struct trace_ctx tctx = { .issue_ts = ts, .q2d_lat = q2d_lat, .pid_tgid = pid_tgid, .issue_cpu = issue_cpu };
    bpf_map_update_elem(&req_start, &req_ptr, &tctx, BPF_ANY);

    // QD 추적: device_qd(글로벌 HASH)에 cross-CPU atomic으로 증감.
    struct gendisk *disk = BPF_CORE_READ(rq, q, disk);
    if (disk) {
        u32 dev = (BPF_CORE_READ(disk, major) << 20) | BPF_CORE_READ(disk, first_minor);

        u64 cmd_flags = BPF_CORE_READ(rq, cmd_flags);
        u32 op = cmd_flags & 255;
        int type = -1;
        if (op == 0) type = (cmd_flags & BPF_REQ_RAHEAD) ? IO_READ_AHEAD : IO_READ;
        else if (op == 1) type = IO_WRITE;
        else if (op == 2) type = IO_FLUSH;
        else if (op == 3) type = IO_DISCARD;

        if (type >= 0 && type < IO_MAX_TYPES) {
            struct dev_qd *q = get_or_init_dev_qd(dev);
            if (q) {
                // BPF XADD는 반환값 사용 불가 → increment 후 별도 read.
                // 두 연산 사이 race가 있지만 max_qd는 soft stat이라 허용.
                __sync_fetch_and_add(&q->current_qd[type], 1);
                int new_qd = q->current_qd[type];
                if (new_qd > 0 && (unsigned int)new_qd > q->max_qd[type]) {
                    q->max_qd[type] = (unsigned int)new_qd;
                }
                // device 전체 in-flight QD 분포 히스토그램.
                int total_qd = 0;
                #pragma unroll
                for (int t = 0; t < IO_MAX_TYPES; t++) total_qd += q->current_qd[t];
                if (total_qd < 0) total_qd = 0;
                u32 qb = (total_qd < QD_HIST_BUCKETS) ? (u32)total_qd
                                                      : (QD_HIST_BUCKETS - 1);
                __sync_fetch_and_add(&q->qd_hist[qb], 1);
            }
        }
    }
    return 0;
}

/*
 * D2C 세분화: nvme_complete_rq tracepoint(=CQ 경계)로 D2C 구간을 둘로 쪼갠다.
 *   block_rq_issue --[D2CQ: device 왕복]--> nvme_complete_rq --[CQ2C]--> block_rq_complete
 * request 포인터를 인자로 받으므로 req_start 맵 키로 그대로 상관.
 * (nvme_setup_cmd는 nvme_queue_rq()에서 block_rq_issue보다 먼저 실행 — D2C 밖이라 안 씀.)
 * nvme tracepoint가 없는 커널에서는 io_trace.c가 best-effort attach로 건너뛴다.
 */
SEC("tp_btf/nvme_complete_rq")
int BPF_PROG(nvme_complete_rq, struct request *req) {
    u64 req_ptr = (u64)req;
    struct trace_ctx *tctx = bpf_map_lookup_elem(&req_start, &req_ptr);
    if (tctx) tctx->cq_ts = bpf_ktime_get_ns();
    return 0;
}

SEC("tp_btf/block_rq_complete")
int BPF_PROG(block_rq_complete, struct request *rq, int error, unsigned int nr_bytes) {
    u64 end_ts = bpf_ktime_get_ns();
    u64 req_ptr = (u64)rq;
    
    struct trace_ctx *tctx = bpf_map_lookup_elem(&req_start, &req_ptr);
    if (!tctx) return 0;

    int type = -1;
    struct gendisk *disk = BPF_CORE_READ(rq, q, disk);
    if (disk) {
        u32 dev = (BPF_CORE_READ(disk, major) << 20) | BPF_CORE_READ(disk, first_minor);
        struct io_stats *s = bpf_map_lookup_elem(&device_stats, &dev);
        u64 d2c_lat = end_ts - tctx->issue_ts;
        u64 q2d_lat = tctx->q2d_lat;

        u64 cmd_flags = BPF_CORE_READ(rq, cmd_flags);
        u32 op = cmd_flags & 255; 

        if (op == 0) type = (cmd_flags & BPF_REQ_RAHEAD) ? IO_READ_AHEAD : IO_READ;
        else if (op == 1) type = IO_WRITE;
        else if (op == 2) type = IO_FLUSH;
        else if (op == 3) type = IO_DISCARD;

        if (type != -1) {
            if (!s) {
                u32 zero_key = 0;
                struct io_stats *init_s = bpf_map_lookup_elem(&scratch_stats, &zero_key);
                if (init_s) {
                    for(int i=0; i<IO_MAX_TYPES; i++) {
                        init_s->stats[i].io_count = 0;
                        init_s->stats[i].total_bytes = 0;
                        init_s->stats[i].q2d.total = 0;
                        init_s->stats[i].q2d.max = 0;
                        init_s->stats[i].q2d.min = (unsigned long long)-1;
                        init_s->stats[i].d2c.total = 0;
                        init_s->stats[i].d2c.max = 0;
                        init_s->stats[i].d2c.min = (unsigned long long)-1;
                        for(int b=0; b<MAX_SIZE_BUCKETS; b++) init_s->stats[i].size_hist[b] = 0;
                        for(int b=0; b<LBA_BUCKETS; b++) init_s->stats[i].lba_hist[b] = 0;
                        for(int b=0; b<LAT_HIST_BUCKETS; b++) {
                            init_s->stats[i].q2d_hist[b] = 0;
                            init_s->stats[i].d2c_hist[b] = 0;
                        }
                        init_s->stats[i].d2cq_total = 0;
                        init_s->stats[i].cq2c_total = 0;
                        init_s->stats[i].d2c_traced_count = 0;
                    }
                    bpf_map_update_elem(&device_stats, &dev, init_s, BPF_ANY);
                    s = bpf_map_lookup_elem(&device_stats, &dev);
                }
            }

            if (s) {
                struct rw_stats *target = &s->stats[type];

                target->io_count++;
                target->total_bytes += nr_bytes;

                if (nr_bytes <= 4096) target->size_hist[0]++;
                else if (nr_bytes <= 32768) target->size_hist[1]++;
                else if (nr_bytes <= 131072) target->size_hist[2]++;
                else target->size_hist[3]++;

                target->d2c.total += d2c_lat;
                if (d2c_lat > target->d2c.max) target->d2c.max = d2c_lat;
                if (target->d2c.min == (unsigned long long)-1 || d2c_lat < target->d2c.min) target->d2c.min = d2c_lat;
                target->d2c_hist[lat_bucket(d2c_lat)]++;

                /* D2C 세분화: nvme_complete_rq(=CQ)를 받은 I/O만. 단조 증가 검증 후
                 * D2CQ(device 왕복) + CQ2C(block 완료) = D2C (놓치는 시간 없음). */
                u64 nct = tctx->cq_ts;
                if (nct > 0 && nct >= tctx->issue_ts && end_ts >= nct) {
                    target->d2cq_total += nct - tctx->issue_ts;
                    target->cq2c_total += end_ts - nct;
                    target->d2c_traced_count++;
                }

                if (q2d_lat > 0) {
                    target->q2d.total += q2d_lat;
                    if (q2d_lat > target->q2d.max) target->q2d.max = q2d_lat;
                    if (target->q2d.min == (unsigned long long)-1 || q2d_lat < target->q2d.min) target->q2d.min = q2d_lat;
                    target->q2d_hist[lat_bucket(q2d_lat)]++;
                }

                u64 sector = BPF_CORE_READ(rq, __sector);
                u64 *cap_ptr = bpf_map_lookup_elem(&dev_capacity_map, &dev);
                if (cap_ptr && *cap_ptr > 0) {
                    u64 bucket = (sector * LBA_BUCKETS) / (*cap_ptr);
                    if (bucket >= LBA_BUCKETS) bucket = LBA_BUCKETS - 1;
                    target->lba_hist[bucket]++;
                }
            }

            // QD 추적: 완료 시 글로벌 카운터에서 감소 (cross-CPU atomic).
            // SQ(issue CPU) vs CQ(현재 CPU) 일치 여부도 동시 집계 — NVMe 큐 affinity 진단용.
            struct dev_qd *qd = bpf_map_lookup_elem(&device_qd, &dev);
            if (qd) {
                __sync_fetch_and_add(&qd->current_qd[type], -1);
                u32 cq_cpu = bpf_get_smp_processor_id();
                if (cq_cpu == tctx->issue_cpu) {
                    __sync_fetch_and_add(&qd->sq_cq_same, 1);
                } else {
                    __sync_fetch_and_add(&qd->sq_cq_diff, 1);
                }
                // issue-CPU x complete-CPU 매트릭스 (sparse HASH).
                u32 mkey = ((tctx->issue_cpu & 0xFFFF) << 16) | (cq_cpu & 0xFFFF);
                u64 *mc = bpf_map_lookup_elem(&cpu_matrix, &mkey);
                if (mc) {
                    __sync_fetch_and_add(mc, 1);
                } else {
                    u64 one = 1;
                    bpf_map_update_elem(&cpu_matrix, &mkey, &one, BPF_NOEXIST);
                }
            }
        }
    }
    
    /* C2R 시작점: bio->bi_private(iomap_dio)에서 kiocb를 꺼내 완료 시각을 저장.
     * libaio·io_uring 모두 같은 맵을 쓰고, 완료측 프로그램(aio_complete /
     * io_uring_complete)이 각자 모드에서만 attach된다. */
    if ((opt_trace_libaio || opt_trace_iouring) && type != -1) {
        struct bio *bio = BPF_CORE_READ(rq, bio);
        if (bio) {
            void *bi_private = BPF_CORE_READ(bio, bi_private);
            if (bi_private) {
                struct kiocb *iocb_ptr = BPF_CORE_READ((struct iomap_dio *)bi_private, iocb);
                if (iocb_ptr) {
                    u64 key = (u64)iocb_ptr;
                    struct comp_ctx cctx = { .ts = end_ts, .type = type, .pid_tgid = tctx->pid_tgid };
                    bpf_map_update_elem(&iocb_comp_start, &key, &cctx, BPF_ANY);
                }
            }
        }
    }
    
    bpf_map_delete_elem(&req_start, &req_ptr);
    return 0;
}
