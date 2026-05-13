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

#define MAX_SIZE_BUCKETS 4
#define LBA_BUCKETS 64

struct lat_stats {
    unsigned long long total;
    unsigned long long max;
    unsigned long long min;
};

struct rw_stats {
    unsigned long long io_count;
    unsigned long long total_bytes;
    struct lat_stats q2d; 
    struct lat_stats d2c; 
    unsigned long long size_hist[MAX_SIZE_BUCKETS];
    unsigned int lba_hist[LBA_BUCKETS]; // LBA 접근 빈도 버킷
    int current_qd;        
    unsigned int max_qd;
};

struct io_stats {
    struct rw_stats stats[IO_MAX_TYPES];
};

struct libaio_stats {
    unsigned long long u2q_count;
    unsigned long long u2q_lat_total;

    unsigned long long c2a_read_count;
    unsigned long long c2a_read_total;
    unsigned long long c2a_write_count;
    unsigned long long c2a_write_total;
    unsigned long long c2a_flush_count;
    unsigned long long c2a_flush_total;

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
