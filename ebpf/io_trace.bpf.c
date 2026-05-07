#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "io_trace.h"

char LICENSE[] SEC("license") = "GPL";
#define BPF_REQ_RAHEAD (1ULL << 19)

/* ====================================================
 * 기존: Q2I & D2C 맵 (Block Layer & Hardware)
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

/* ====================================================
 * 신규 개선: libaio U2Q & C2U 맵 (동시성/Race Condition 해결)
 * ==================================================== */
// 스레드 단위(TID) 제출 시간 추적
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10240);
    __type(key, u64); // pid_tgid (전체 64비트 TID)
    __type(value, u64); // ts
} pid_submit_start SEC(".maps");

// io_getevents 진입 시 TID와 ctx_id 매핑
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10240);
    __type(key, u64); // pid_tgid
    __type(value, u64); // ctx_id
} active_getevents SEC(".maps");

// AIO 컨텍스트별 마지막 하드웨어 완료 시간
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10240);
    __type(key, u64); // ctx_id (kioctx pointer)
    __type(value, u64); // ts
} ctx_last_complete SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, struct libaio_stats);
} sys_stats_map SEC(".maps");


/* ====================================================
 * HOOK: Libaio 상단 (U2Q - 제출 오버헤드 측정)
 * ==================================================== */
SEC("tracepoint/syscalls/sys_enter_io_submit")
int trace_submit_enter(void *ctx) {
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid(); // 64비트 TID 사용 (동시성 해결)
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

/* (이하 블록 레이어 Q2I, D2C 로직은 기존과 동일하므로 생략 없이 원본 유지) */
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
 * HOOK: Libaio 하단 (C2U - Wakeup 지연 측정 정밀화)
 * ==================================================== */

// 1. io_getevents 진입 시 현재 스레드가 대기하려는 ctx_id 저장
SEC("tracepoint/syscalls/sys_enter_io_getevents")
int trace_getevents_enter(struct trace_event_raw_sys_enter *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 ctx_id = ctx->args[0]; // aio_context_t 매개변수 추출
    bpf_map_update_elem(&active_getevents, &pid_tgid, &ctx_id, BPF_ANY);
    return 0;
}

// 2. 하드웨어 인터럽트로 aio_complete 호출 시 해당 ctx_id에 시간 기록
SEC("kprobe/aio_complete")
int BPF_KPROBE(trace_aio_complete, struct kiocb *iocb) {
    u64 ts = bpf_ktime_get_ns();
    struct aio_kiocb *aio_req = (struct aio_kiocb *)iocb;
    
    // [수정된 부분]
    // ki_ctx(커널 포인터) 자체가 아니라, ki_ctx 내부의 user_id를 읽어와야 
    // 유저 스페이스의 io_getevents syscall이 사용하는 ctx_id와 정확히 일치합니다.
    u64 ctx_id = (u64)BPF_CORE_READ(aio_req, ki_ctx, user_id);
    
    if (ctx_id) {
        bpf_map_update_elem(&ctx_last_complete, &ctx_id, &ts, BPF_ANY);
    }
    return 0;
}

// 3. io_getevents 반환 시, 매핑된 ctx_id의 완료 시간과 비교하여 딜레이 산출
SEC("tracepoint/syscalls/sys_exit_io_getevents")
int trace_getevents_exit(struct trace_event_raw_sys_exit *ctx) {
    if (ctx->ret <= 0) return 0; // 이벤트를 수거하지 못했으면 제외

    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    
    u64 *ctx_id_ptr = bpf_map_lookup_elem(&active_getevents, &pid_tgid);
    if (ctx_id_ptr) {
        u64 *last_ts = bpf_map_lookup_elem(&ctx_last_complete, ctx_id_ptr);
        
        if (last_ts && *last_ts > 0 && ts > *last_ts) {
            u64 wakeup_lat = ts - *last_ts;
            u32 key = 0;
            struct libaio_stats *st = bpf_map_lookup_elem(&sys_stats_map, &key);
            
            if (st) {
                __sync_fetch_and_add(&st->getevents_count, 1);
                __sync_fetch_and_add(&st->wakeup_lat_total, wakeup_lat);
                if (wakeup_lat > st->wakeup_lat_max) st->wakeup_lat_max = wakeup_lat;
            }
        }
        bpf_map_delete_elem(&active_getevents, &pid_tgid); // 측정 완료 후 정리
    }
    return 0;
}

// 1. 최신 커널에서 aio_complete 대신 aio_complete_rw를 거칠 수 있음
SEC("kprobe/aio_complete_rw")
int BPF_KPROBE(trace_aio_complete_rw, struct kiocb *iocb) {
    u64 ts = bpf_ktime_get_ns();
    struct aio_kiocb *aio_req = (struct aio_kiocb *)iocb;
    u64 ctx_id = (u64)BPF_CORE_READ(aio_req, ki_ctx, user_id);
    if (ctx_id) bpf_map_update_elem(&ctx_last_complete, &ctx_id, &ts, BPF_ANY);
    return 0;
}

// 2. ARM64 환경 등에서 pgetevents 시스템 콜을 탈 경우를 대비
SEC("tracepoint/syscalls/sys_enter_io_pgetevents")
int trace_pgetevents_enter(struct trace_event_raw_sys_enter *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 ctx_id = ctx->args[0]; 
    bpf_map_update_elem(&active_getevents, &pid_tgid, &ctx_id, BPF_ANY);
    return 0;
}

SEC("tracepoint/syscalls/sys_exit_io_pgetevents")
int trace_pgetevents_exit(struct trace_event_raw_sys_exit *ctx) {
    if (ctx->ret <= 0) return 0; 

    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    
    u64 *ctx_id_ptr = bpf_map_lookup_elem(&active_getevents, &pid_tgid);
    if (ctx_id_ptr) {
        u64 *last_ts = bpf_map_lookup_elem(&ctx_last_complete, ctx_id_ptr);
        
        if (last_ts && *last_ts > 0 && ts > *last_ts) {
            u64 wakeup_lat = ts - *last_ts;
            u32 key = 0;
            struct libaio_stats *st = bpf_map_lookup_elem(&sys_stats_map, &key);
            
            if (st) {
                __sync_fetch_and_add(&st->getevents_count, 1);
                __sync_fetch_and_add(&st->wakeup_lat_total, wakeup_lat);
                if (wakeup_lat > st->wakeup_lat_max) st->wakeup_lat_max = wakeup_lat;
            }
        }
        bpf_map_delete_elem(&active_getevents, &pid_tgid);
    }
    return 0;
}