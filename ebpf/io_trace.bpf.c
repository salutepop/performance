#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "io_trace.h"

char LICENSE[] SEC("license") = "GPL";
#define BPF_REQ_RAHEAD (1ULL << 19)

/* ====================================================
 * 블록 레이어 맵 및 libaio 맵
 * ==================================================== */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); 
    __type(value, u64); 
} bio_start SEC(".maps");

struct trace_ctx {
    u64 issue_ts;
    u64 q2i_lat;
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

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); 
    __type(value, u64); 
} iocb_complete_ts SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct libaio_stats);
} sys_stats_map SEC(".maps");

/* ====================================================
 * HOOK: Libaio 상단 (U2Q) & Block Layer (Q2I, D2C)
 * ==================================================== */
SEC("tracepoint/syscalls/sys_enter_io_submit")
int trace_submit_enter(void *ctx) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    bpf_map_update_elem(&pid_submit_start, &pid_tgid, &ts, BPF_ANY);
    return 0;
}

SEC("tracepoint/syscalls/sys_exit_io_submit")
int trace_submit_exit(void *ctx) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 *start = bpf_map_lookup_elem(&pid_submit_start, &pid_tgid);
    
    if (start) {
        u64 lat = ts - *start;
        u32 key = 0;
        struct libaio_stats *st = bpf_map_lookup_elem(&sys_stats_map, &key);
        if (st) {
            __sync_fetch_and_add(&st->submit_count, 1);
            __sync_fetch_and_add(&st->submit_lat_total, lat);
            if (lat > st->submit_lat_max) st->submit_lat_max = lat;
        }
        bpf_map_delete_elem(&pid_submit_start, &pid_tgid);
    }
    return 0;
}

SEC("tp_btf/block_bio_queue")
int BPF_PROG(block_bio_queue, struct bio *bio) {
    u64 ts = bpf_ktime_get_ns();
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
    
    u64 q2i_lat = 0;
    if (bio_ptr != 0) {
        u64 *b_ts = bpf_map_lookup_elem(&bio_start, &bio_ptr);
        if (b_ts) {
            q2i_lat = ts - *b_ts;
            bpf_map_delete_elem(&bio_start, &bio_ptr);
        }
    }

    struct trace_ctx tctx = { .issue_ts = ts, .q2i_lat = q2i_lat };
    bpf_map_update_elem(&req_start, &req_ptr, &tctx, BPF_ANY);
    return 0;
}

SEC("tp_btf/block_rq_complete")
int BPF_PROG(block_rq_complete, struct request *rq, int error, unsigned int nr_bytes) {
    u64 end_ts = bpf_ktime_get_ns();
    u64 req_ptr = (u64)rq;
    
    struct trace_ctx *tctx = bpf_map_lookup_elem(&req_start, &req_ptr);
    if (!tctx) return 0;

    struct gendisk *disk = BPF_CORE_READ(rq, q, disk);
    if (disk) {
        u32 dev = (BPF_CORE_READ(disk, major) << 20) | BPF_CORE_READ(disk, first_minor);
        struct io_stats *s = bpf_map_lookup_elem(&device_stats, &dev);
        u64 d2c_lat = end_ts - tctx->issue_ts;
        u64 q2i_lat = tctx->q2i_lat;

        u64 cmd_flags = BPF_CORE_READ(rq, cmd_flags);
        u32 op = cmd_flags & 255; 

        int type = -1;
        if (op == 0) type = (cmd_flags & BPF_REQ_RAHEAD) ? IO_READ_AHEAD : IO_READ;
        else if (op == 1) type = IO_WRITE;
        else if (op == 2) type = IO_FLUSH;
        else if (op == 3) type = IO_DISCARD;

        if (type != -1) {
            struct io_stats new_s = {};
            if (!s) {
                for(int i=0; i<IO_MAX_TYPES; i++) {
                    new_s.stats[i].q2i.min = (unsigned long long)-1;
                    new_s.stats[i].d2c.min = (unsigned long long)-1;
                }
            }
            struct rw_stats *target = s ? &s->stats[type] : &new_s.stats[type];
            target->io_count++;
            target->total_bytes += nr_bytes;
            
            target->d2c.total += d2c_lat;
            if (d2c_lat > target->d2c.max) target->d2c.max = d2c_lat;
            if (target->d2c.min == (unsigned long long)-1 || d2c_lat < target->d2c.min) target->d2c.min = d2c_lat;

            if (q2i_lat > 0) {
                target->q2i.total += q2i_lat;
                if (q2i_lat > target->q2i.max) target->q2i.max = q2i_lat;
                if (target->q2i.min == (unsigned long long)-1 || q2i_lat < target->q2i.min) target->q2i.min = q2i_lat;
            }
            if (!s) bpf_map_update_elem(&device_stats, &dev, &new_s, BPF_ANY);
        }
    }
    bpf_map_delete_elem(&req_start, &req_ptr);
    return 0;
}

/* ====================================================
 * HOOK: Libaio 하단 (C2U - 개별 iocb consume 레이턴시)
 * ==================================================== */
SEC("kprobe/aio_complete")
int trace_aio_complete(struct pt_regs *ctx) {
    struct aio_kiocb *iocb = (struct aio_kiocb *)PT_REGS_PARM1(ctx);
    u64 key = BPF_CORE_READ(iocb, ki_res.obj);
    if (key) {
        u64 ts = bpf_ktime_get_ns();
        bpf_map_update_elem(&iocb_complete_ts, &key, &ts, BPF_ANY);
    }
    return 0;
}

SEC("kprobe/aio_complete_rw")
int trace_aio_complete_rw(struct pt_regs *ctx) {
    struct aio_kiocb *iocb = (struct aio_kiocb *)PT_REGS_PARM1(ctx);
    u64 key = BPF_CORE_READ(iocb, ki_res.obj);
    if (key) {
        u64 ts = bpf_ktime_get_ns();
        bpf_map_update_elem(&iocb_complete_ts, &key, &ts, BPF_ANY);
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
                __sync_fetch_and_add(&st->getevents_count, 1);
                __sync_fetch_and_add(&st->wakeup_lat_total, wakeup_lat);
                if (wakeup_lat > st->wakeup_lat_max) st->wakeup_lat_max = wakeup_lat;

                // Userspace의 iocb 구조체에서 16번째 바이트(aio_lio_opcode)를 추출하여 명령어 판별
                u16 opcode = 0;
                bpf_probe_read_user(&opcode, sizeof(opcode), (void *)(iocb_ptr + 16));

                // 0: PREAD, 7: PREADV
                if (opcode == 0 || opcode == 7) {
                    __sync_fetch_and_add(&st->c2u_read_count, 1);
                    __sync_fetch_and_add(&st->c2u_read_total, wakeup_lat);
                // 1: PWRITE, 8: PWRITEV
                } else if (opcode == 1 || opcode == 8) {
                    __sync_fetch_and_add(&st->c2u_write_count, 1);
                    __sync_fetch_and_add(&st->c2u_write_total, wakeup_lat);
                // 2: FSYNC, 3: FDSYNC
                } else if (opcode == 2 || opcode == 3) {
                    __sync_fetch_and_add(&st->c2u_flush_count, 1);
                    __sync_fetch_and_add(&st->c2u_flush_total, wakeup_lat);
                }
            }
            bpf_map_delete_elem(&iocb_complete_ts, &iocb_ptr);
        }
    }
    return 0;
}

// pgetevents용 백업 루틴
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
                __sync_fetch_and_add(&st->getevents_count, 1);
                __sync_fetch_and_add(&st->wakeup_lat_total, wakeup_lat);
                if (wakeup_lat > st->wakeup_lat_max) st->wakeup_lat_max = wakeup_lat;

                u16 opcode = 0;
                bpf_probe_read_user(&opcode, sizeof(opcode), (void *)(iocb_ptr + 16));

                if (opcode == 0 || opcode == 7) {
                    __sync_fetch_and_add(&st->c2u_read_count, 1);
                    __sync_fetch_and_add(&st->c2u_read_total, wakeup_lat);
                } else if (opcode == 1 || opcode == 8) {
                    __sync_fetch_and_add(&st->c2u_write_count, 1);
                    __sync_fetch_and_add(&st->c2u_write_total, wakeup_lat);
                } else if (opcode == 2 || opcode == 3) {
                    __sync_fetch_and_add(&st->c2u_flush_count, 1);
                    __sync_fetch_and_add(&st->c2u_flush_total, wakeup_lat);
                }
            }
            bpf_map_delete_elem(&iocb_complete_ts, &iocb_ptr);
        }
    }
    return 0;
}