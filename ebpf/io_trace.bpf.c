#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "io_trace.h"

char LICENSE[] SEC("license") = "GPL";

// 최신 리눅스 커널(5.x 이상)에서 REQ_RAHEAD(Read-Ahead)를 의미하는 비트 플래그입니다.
#define BPF_REQ_RAHEAD (1ULL << 19)

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1048576);
    __type(key, u64);
    __type(value, u64);
} start_times SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __uint(max_entries, 256);
    __type(key, u32);
    __type(value, struct io_stats);
} device_stats SEC(".maps");

SEC("tp_btf/block_rq_issue")
int BPF_PROG(block_rq_issue, struct request *rq) {
    u64 ts = bpf_ktime_get_ns();
    u64 req_ptr = (u64)rq;
    bpf_map_update_elem(&start_times, &req_ptr, &ts, BPF_ANY);
    return 0;
}

SEC("tp_btf/block_rq_complete")
int BPF_PROG(block_rq_complete, struct request *rq, int error, unsigned int nr_bytes) {
    u64 end_ts = bpf_ktime_get_ns();
    u64 req_ptr = (u64)rq;
    u64 *start_ts = bpf_map_lookup_elem(&start_times, &req_ptr);

    if (!start_ts) return 0;

    struct gendisk *disk = BPF_CORE_READ(rq, q, disk);
    if (disk) {
        u32 dev = (BPF_CORE_READ(disk, major) << 20) | BPF_CORE_READ(disk, first_minor);
        struct io_stats *s = bpf_map_lookup_elem(&device_stats, &dev);
        u64 lat = end_ts - *start_ts;

        // cmd_flags에서 실제 Operation Type 추출
        u64 cmd_flags = BPF_CORE_READ(rq, cmd_flags);
        u32 op = cmd_flags & 255; 

        int type = -1;
        if (op == 0) { // REQ_OP_READ
            if (cmd_flags & BPF_REQ_RAHEAD) type = IO_READ_AHEAD;
            else type = IO_READ;
        } else if (op == 1) { // REQ_OP_WRITE
            type = IO_WRITE;
        } else if (op == 2) { // REQ_OP_FLUSH
            type = IO_FLUSH;
        } else if (op == 3) { // REQ_OP_DISCARD
            type = IO_DISCARD;
        }

        // 해당하는 I/O 타입이 있으면 배열에 업데이트
        if (type != -1) {
            if (s) {
                s->stats[type].io_count++;
                s->stats[type].total_latency += lat;
                s->stats[type].total_bytes += nr_bytes;
                if (lat > s->stats[type].max_latency) s->stats[type].max_latency = lat;
                if (s->stats[type].min_latency == 0 || lat < s->stats[type].min_latency) 
                    s->stats[type].min_latency = lat;
            } else {
                struct io_stats new_s = {};
                new_s.stats[type].io_count = 1;
                new_s.stats[type].total_latency = lat;
                new_s.stats[type].total_bytes = nr_bytes;
                new_s.stats[type].max_latency = lat;
                new_s.stats[type].min_latency = lat;
                bpf_map_update_elem(&device_stats, &dev, &new_s, BPF_ANY);
            }
        }
    }
    bpf_map_delete_elem(&start_times, &req_ptr);
    return 0;
}
