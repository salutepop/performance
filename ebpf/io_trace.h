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
    struct lat_stats q2i; // Queue-to-Issue (블록 대기)
    struct lat_stats d2c; // Issue-to-Complete (하드웨어 성능)
};

struct io_stats {
    struct rw_stats stats[IO_MAX_TYPES];
};

// libaio 시스템 콜 오버헤드 추적을 위한 구조체
struct libaio_stats {
    unsigned long long submit_count;
    unsigned long long submit_lat_total;
    unsigned long long submit_lat_max;

    unsigned long long getevents_count;
    unsigned long long wakeup_lat_total;
    unsigned long long wakeup_lat_max;
};

// BPF 컴파일 환경에서는 vmlinux.h의 구조체를 사용하도록 예외 처리
#ifndef __BPF__
// libaio io_event 구조체 (userspace ABI)
struct io_event {
    __u64 data;
    __u64 obj;  // 이게 커널의 struct kiocb * 포인터와 동일함
    __s64 res;
    __s64 res2;
};
#endif

#endif