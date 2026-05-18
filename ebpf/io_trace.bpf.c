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
};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); 
    __type(value, struct trace_ctx);
} req_start SEC(".maps");

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

struct c2a_ctx {
    u64 ts;
    int type;
    u64 pid_tgid;
};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); 
    __type(value, struct c2a_ctx); 
} iocb_c2a_start SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct libaio_stats);
} sys_stats_map SEC(".maps");

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

static __always_inline struct dev_qd *get_or_init_dev_qd(u32 dev) {
    struct dev_qd *q = bpf_map_lookup_elem(&device_qd, &dev);
    if (q) return q;
    struct dev_qd init = {};
    bpf_map_update_elem(&device_qd, &dev, &init, BPF_NOEXIST);
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
    struct c2a_ctx *cctx = bpf_map_lookup_elem(&iocb_c2a_start, &key_iocb);
    if (cctx && cctx->ts > 0) {
        pid_tgid = cctx->pid_tgid; 
        if (ts > cctx->ts) {
            u64 c2a_lat = ts - cctx->ts;
            u32 stat_key = 0;
            struct libaio_stats *st = bpf_map_lookup_elem(&sys_stats_map, &stat_key);
            if (st) {
                if (cctx->type == IO_READ || cctx->type == IO_READ_AHEAD) {
                    __sync_fetch_and_add(&st->c2a_read_count, 1);
                    __sync_fetch_and_add(&st->c2a_read_total, c2a_lat);
                } else if (cctx->type == IO_WRITE) {
                    __sync_fetch_and_add(&st->c2a_write_count, 1);
                    __sync_fetch_and_add(&st->c2a_write_total, c2a_lat);
                } else if (cctx->type == IO_FLUSH) {
                    __sync_fetch_and_add(&st->c2a_flush_count, 1);
                    __sync_fetch_and_add(&st->c2a_flush_total, c2a_lat);
                }
            }
        }
        bpf_map_delete_elem(&iocb_c2a_start, &key_iocb);
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
    struct libaio_stats *st = bpf_map_lookup_elem(&sys_stats_map, &key);
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
                    __sync_fetch_and_add(&st->a2u_read_count, 1);
                    __sync_fetch_and_add(&st->a2u_read_total, wakeup_lat);
                } else if (opcode == 1 || opcode == 8) {
                    __sync_fetch_and_add(&st->a2u_write_count, 1);
                    __sync_fetch_and_add(&st->a2u_write_total, wakeup_lat);
                } else if (opcode == 2 || opcode == 3) {
                    __sync_fetch_and_add(&st->a2u_flush_count, 1);
                    __sync_fetch_and_add(&st->a2u_flush_total, wakeup_lat);
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
    struct libaio_stats *st = bpf_map_lookup_elem(&sys_stats_map, &key);
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
                    __sync_fetch_and_add(&st->a2u_read_count, 1);
                    __sync_fetch_and_add(&st->a2u_read_total, wakeup_lat);
                } else if (opcode == 1 || opcode == 8) {
                    __sync_fetch_and_add(&st->a2u_write_count, 1);
                    __sync_fetch_and_add(&st->a2u_write_total, wakeup_lat);
                } else if (opcode == 2 || opcode == 3) {
                    __sync_fetch_and_add(&st->a2u_flush_count, 1);
                    __sync_fetch_and_add(&st->a2u_flush_total, wakeup_lat);
                }
            }
            bpf_map_delete_elem(&iocb_complete_ts, &akey);
        }
    }
    return 0;
}

SEC("tp_btf/block_bio_queue")
int BPF_PROG(block_bio_queue, struct bio *bio) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    if (opt_trace_libaio) {
        u64 *submit_ts = bpf_map_lookup_elem(&pid_submit_start, &pid_tgid);
        if (submit_ts) {
            u64 u2q_lat = ts - *submit_ts;
            u32 key = 0;
            struct libaio_stats *st = bpf_map_lookup_elem(&sys_stats_map, &key);
            if (st) {
                __sync_fetch_and_add(&st->u2q_count, 1);
                __sync_fetch_and_add(&st->u2q_lat_total, u2q_lat);
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

    struct trace_ctx tctx = { .issue_ts = ts, .q2d_lat = q2d_lat, .pid_tgid = pid_tgid };
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
            }
        }
    }
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
            struct dev_qd *qd = bpf_map_lookup_elem(&device_qd, &dev);
            if (qd) {
                __sync_fetch_and_add(&qd->current_qd[type], -1);
            }
        }
    }
    
    if (opt_trace_libaio && type != -1) {
        struct bio *bio = BPF_CORE_READ(rq, bio);
        if (bio) {
            void *bi_private = BPF_CORE_READ(bio, bi_private);
            if (bi_private) {
                struct kiocb *iocb_ptr = BPF_CORE_READ((struct iomap_dio *)bi_private, iocb);
                if (iocb_ptr) {
                    u64 key = (u64)iocb_ptr;
                    struct c2a_ctx cctx = { .ts = end_ts, .type = type, .pid_tgid = tctx->pid_tgid };
                    bpf_map_update_elem(&iocb_c2a_start, &key, &cctx, BPF_ANY);
                }
            }
        }
    }
    
    bpf_map_delete_elem(&req_start, &req_ptr);
    return 0;
}
