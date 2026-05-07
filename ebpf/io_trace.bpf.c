#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "io_trace.h"

char LICENSE[] SEC("license") = "GPL";
#define BPF_REQ_RAHEAD (1ULL << 19)

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); 
    __type(value, u64); 
} bio_start SEC(".maps");

struct trace_ctx {
    u64 issue_ts;
    u64 q2d_lat;
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

// 시스템 콜 시작점 추적 (U2Q 계산용)
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

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); 
    __type(value, u64); 
} iocb_complete_ts SEC(".maps");

struct c2a_ctx {
    u64 ts;
    int type;
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

SEC("tracepoint/syscalls/sys_enter_io_submit")
int trace_submit_enter(void *ctx) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    bpf_map_update_elem(&pid_submit_start, &pid_tgid, &ts, BPF_ANY);
    return 0;
}

SEC("tracepoint/syscalls/sys_exit_io_submit")
int trace_submit_exit(void *ctx) {
    // 메모리 누수 방지용 삭제만 수행 (U2Q 측정은 bio_queue에서 완료됨)
    u64 pid_tgid = bpf_get_current_pid_tgid();
    bpf_map_delete_elem(&pid_submit_start, &pid_tgid);
    return 0;
}

SEC("tp_btf/block_bio_queue")
int BPF_PROG(block_bio_queue, struct bio *bio) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    
    // 1. [U2Q] 개별 BIO 단위 제출 지연시간 측정
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

    // 2. [Q2D] 큐 대기시간 시작점 마킹
    u64 bio_ptr = (u64)bio;
    bpf_map_update_elem(&bio_start, &bio_ptr, &ts, BPF_ANY);
    return 0;
}

SEC("tp_btf/block_rq_issue")
int BPF_PROG(block_rq_issue, struct request *rq) {
    u64 ts = bpf_ktime_get_ns();
    u64 req_ptr = (u64)rq;
    struct bio *bio = BPF_CORE_READ(rq, bio);
    u64 bio_ptr = (u64)bio;
    
    u64 q2d_lat = 0;
    if (bio_ptr != 0) {
        u64 *b_ts = bpf_map_lookup_elem(&bio_start, &bio_ptr);
        if (b_ts) {
            q2d_lat = ts - *b_ts;
            bpf_map_delete_elem(&bio_start, &bio_ptr);
        }
    }

    struct trace_ctx tctx = { .issue_ts = ts, .q2d_lat = q2d_lat };
    bpf_map_update_elem(&req_start, &req_ptr, &tctx, BPF_ANY);
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
            struct io_stats new_s = {};
            if (!s) {
                for(int i=0; i<IO_MAX_TYPES; i++) {
                    new_s.stats[i].q2d.min = (unsigned long long)-1;
                    new_s.stats[i].d2c.min = (unsigned long long)-1;
                }
            }
            struct rw_stats *target = s ? &s->stats[type] : &new_s.stats[type];
            target->io_count++;
            target->total_bytes += nr_bytes;
            
            target->d2c.total += d2c_lat;
            if (d2c_lat > target->d2c.max) target->d2c.max = d2c_lat;
            if (target->d2c.min == (unsigned long long)-1 || d2c_lat < target->d2c.min) target->d2c.min = d2c_lat;

            if (q2d_lat > 0) {
                target->q2d.total += q2d_lat;
                if (q2d_lat > target->q2d.max) target->q2d.max = q2d_lat;
                if (target->q2d.min == (unsigned long long)-1 || q2d_lat < target->q2d.min) target->q2d.min = q2d_lat;
            }
            if (!s) bpf_map_update_elem(&device_stats, &dev, &new_s, BPF_ANY);
        }
    }
    
    // C2A 시작점 마킹 (명령어 타입 함께 저장)
    if (type != -1) {
        struct bio *bio = BPF_CORE_READ(rq, bio);
        if (bio) {
            void *bi_private = BPF_CORE_READ(bio, bi_private);
            if (bi_private) {
                struct kiocb *iocb_ptr = BPF_CORE_READ((struct iomap_dio *)bi_private, iocb);
                if (iocb_ptr) {
                    u64 key = (u64)iocb_ptr;
                    struct c2a_ctx cctx = { .ts = end_ts, .type = type };
                    bpf_map_update_elem(&iocb_c2a_start, &key, &cctx, BPF_ANY);
                }
            }
        }
    }
    
    bpf_map_delete_elem(&req_start, &req_ptr);
    return 0;
}

SEC("kprobe/aio_complete")
int trace_aio_complete(struct pt_regs *ctx) {
    struct aio_kiocb *aio_iocb = (struct aio_kiocb *)PT_REGS_PARM1(ctx);
    u64 ts = bpf_ktime_get_ns();

    // 1. C2A 계산
    u64 key_iocb = (u64)aio_iocb; 
    struct c2a_ctx *cctx = bpf_map_lookup_elem(&iocb_c2a_start, &key_iocb);
    if (cctx && cctx->ts > 0 && ts > cctx->ts) {
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
        bpf_map_delete_elem(&iocb_c2a_start, &key_iocb);
    }

    // 2. A2U 시작점 마킹
    u64 key_user = BPF_CORE_READ(aio_iocb, ki_res.obj);
    if (key_user) {
        bpf_map_update_elem(&iocb_complete_ts, &key_user, &ts, BPF_ANY);
    }
    return 0;
}

SEC("kprobe/aio_complete_rw")
int trace_aio_complete_rw(struct pt_regs *ctx) {
    struct aio_kiocb *aio_iocb = (struct aio_kiocb *)PT_REGS_PARM1(ctx);
    u64 ts = bpf_ktime_get_ns();

    u64 key_iocb = (u64)aio_iocb; 
    struct c2a_ctx *cctx = bpf_map_lookup_elem(&iocb_c2a_start, &key_iocb);
    if (cctx && cctx->ts > 0 && ts > cctx->ts) {
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
        bpf_map_delete_elem(&iocb_c2a_start, &key_iocb);
    }

    u64 key_user = BPF_CORE_READ(aio_iocb, ki_res.obj);
    if (key_user) {
        bpf_map_update_elem(&iocb_complete_ts, &key_user, &ts, BPF_ANY);
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
            u64 *last_ts = bpf_map_lookup_elem(&iocb_complete_ts, &iocb_ptr);
            
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
            bpf_map_delete_elem(&iocb_complete_ts, &iocb_ptr);
        }
    }
    return 0;
}

// pgetevents 백업 (ARM64 등)
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
            u64 *last_ts = bpf_map_lookup_elem(&iocb_complete_ts, &iocb_ptr);
            
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
            bpf_map_delete_elem(&iocb_complete_ts, &iocb_ptr);
        }
    }
    return 0;
}