#ifndef __IO_TRACE_H
#define __IO_TRACE_H

#ifdef __BPF__
    typedef unsigned __int128 __uint128_t;
#else
    #include <linux/types.h>
#endif

enum io_req_type {
    IO_READ = 0,
    IO_READ_AHEAD,
    IO_WRITE,
    IO_FLUSH,
    IO_DISCARD,
    IO_MAX_TYPES
};

struct lat_stats {
    unsigned long long total;
    unsigned long long max;
    unsigned long long min;
};

struct rw_stats {
    unsigned long long io_count;
    unsigned long long total_bytes;
    struct lat_stats q2d; // Queue to Dispatch
    struct lat_stats d2c; // Dispatch to Complete
};

struct io_stats {
    struct rw_stats stats[IO_MAX_TYPES];
};

struct libaio_stats {
    // 1. U2Q: 개별 IO 단위의 Submit Latency (Tail-biting)
    unsigned long long u2q_count;
    unsigned long long u2q_lat_total;

    // 4. C2A (Complete to AIO): FS End-IO 메타데이터 처리 지연
    unsigned long long c2a_read_count;
    unsigned long long c2a_read_total;
    unsigned long long c2a_write_count;
    unsigned long long c2a_write_total;
    unsigned long long c2a_flush_count;
    unsigned long long c2a_flush_total;

    // 5. A2U (AIO to User): User Wakeup 지연
    unsigned long long a2u_read_count;
    unsigned long long a2u_read_total;
    unsigned long long a2u_write_count;
    unsigned long long a2u_write_total;
    unsigned long long a2u_flush_count;
    unsigned long long a2u_flush_total;
};

#ifndef __BPF__
struct io_event {
    __u64 data;
    __u64 obj;  
    __s64 res;
    __s64 res2;
};
#endif

#endif