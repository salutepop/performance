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

// 지연 시간 통계를 담는 서브 구조체
struct lat_stats {
    unsigned long long total;
    unsigned long long max;
    unsigned long long min;
};

// 개별 I/O 타입의 통계 (Q2I와 D2C를 완벽히 분리)
struct rw_stats {
    unsigned long long io_count;
    unsigned long long total_bytes;
    struct lat_stats q2i; // Queue-to-Issue (OS 오버헤드)
    struct lat_stats d2c; // Issue-to-Complete (순수 하드웨어 지연)
};

// 장치 하나가 5가지 I/O 타입의 통계를 모두 가집니다.
struct io_stats {
    struct rw_stats stats[IO_MAX_TYPES];
};

#endif
