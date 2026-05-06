#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "io_trace.h"

char LICENSE[] SEC("license") = "GPL";
#define BPF_REQ_RAHEAD (1ULL << 19)

// 1. T1: bio 큐 진입 시간 저장 맵 (OS 대기 시작점)
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); // bio_ptr
    __type(value, u64); // timestamp
} bio_start SEC(".maps");

// 2. T2: req 전송 시간(D2C 시작점) & 계산된 Q2I 지연시간 저장 맵
struct trace_ctx {
    u64 issue_ts;
    u64 q2i_lat;
};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64); // req_ptr
    __type(value, struct trace_ctx);
} req_start SEC(".maps");

// 3. 최종 통계 저장 맵
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __uint(max_entries, 256);
    __type(key, u32); // dev_id
    __type(value, struct io_stats);
} device_stats SEC(".maps");


// [HOOK 1] OS 블록 레이어 큐에 진입 (Q2I 시작)
SEC("tp_btf/block_bio_queue")
int BPF_PROG(block_bio_queue, struct bio *bio) {
    u64 ts = bpf_ktime_get_ns();
    u64 bio_ptr = (u64)bio;
    bpf_map_update_elem(&bio_start, &bio_ptr, &ts, BPF_ANY);
    return 0;
}

// [HOOK 2] 디바이스 드라이버로 I/O Issue (Q2I 끝, D2C 시작)
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

    // [수정 포인트] ctx 변수명을 tctx로 변경하여 이름 충돌 방지
    struct trace_ctx tctx = {
        .issue_ts = ts,
        .q2i_lat = q2i_lat
    };
    bpf_map_update_elem(&req_start, &req_ptr, &tctx, BPF_ANY);
    return 0;
}

// [HOOK 3] 하드웨어가 처리를 완료 (D2C 끝)
SEC("tp_btf/block_rq_complete")
int BPF_PROG(block_rq_complete, struct request *rq, int error, unsigned int nr_bytes) {
    u64 end_ts = bpf_ktime_get_ns();
    u64 req_ptr = (u64)rq;
    
    // [수정 포인트] ctx 변수명을 tctx로 변경
    struct trace_ctx *tctx = bpf_map_lookup_elem(&req_start, &req_ptr);
    if (!tctx) return 0;

    struct gendisk *disk = BPF_CORE_READ(rq, q, disk);
    if (disk) {
        u32 dev = (BPF_CORE_READ(disk, major) << 20) | BPF_CORE_READ(disk, first_minor);
        struct io_stats *s = bpf_map_lookup_elem(&device_stats, &dev);
        
        // [수정 포인트] tctx 로 접근
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
            struct rw_stats *target = s ? &s->stats[type] : &new_s.stats[type];

            if (!s) {
                target->q2i.min = (unsigned long long)-1;
                target->d2c.min = (unsigned long long)-1;
            }

            target->io_count++;
            target->total_bytes += nr_bytes;
            
            // D2C (순수 HW 지연) 갱신
            target->d2c.total += d2c_lat;
            if (d2c_lat > target->d2c.max) target->d2c.max = d2c_lat;
            if (target->d2c.min == (unsigned long long)-1 || d2c_lat < target->d2c.min) target->d2c.min = d2c_lat;

            // Q2I (OS 큐잉 지연) 갱신
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
