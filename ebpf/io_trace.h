#ifndef __IO_TRACE_H
#define __IO_TRACE_H

#ifdef __BPF__
    typedef unsigned __int128 __uint128_t;
#else
    #include <linux/types.h>
#endif

// 리눅스 블록 레이어의 주요 I/O 타입 5가지
enum io_req_type {
    IO_READ = 0,
    IO_READ_AHEAD,
    IO_WRITE,
    IO_FLUSH,
    IO_DISCARD,
    IO_MAX_TYPES
};

// 개별 I/O 타입의 통계
struct rw_stats {
    unsigned long long io_count;
    unsigned long long total_latency;
    unsigned long long total_bytes;
    unsigned long long max_latency;
    unsigned long long min_latency;
};

// 장치 하나가 5가지 I/O 타입의 통계를 모두 가집니다.
struct io_stats {
    struct rw_stats stats[IO_MAX_TYPES];
};

#endif
