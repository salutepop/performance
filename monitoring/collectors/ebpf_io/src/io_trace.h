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
#define LBA_BUCKETS 128   // 0..LBA_BUCKETS-1 (각 bucket = capacity_sectors / LBA_BUCKETS)
#define LAT_HIST_BUCKETS 32   // log2(ns) buckets: 0=[1,2)ns ... 30=~1s. clamp to 31.
#define QD_HIST_BUCKETS 64    // device queue-depth histogram: bucket = min(total in-flight, 63)
#define DISK_NAME_LEN 32      // 커널 gendisk.disk_name 길이

/*
 * 디바이스의 커널 disk_name (gendisk.disk_name). block 계층 tracepoint가 보는
 * gendisk에서 BPF가 직접 읽어 보고한다 — NVMe 멀티패스의 hidden path device
 * (nvmeXcYnZ)처럼 /sys/dev/block 엔트리가 없는 디바이스도 실명으로 식별된다.
 */
struct dev_name {
    char name[DISK_NAME_LEN];
};

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
    unsigned long long q2d_hist[LAT_HIST_BUCKETS]; // log2(ns) latency 히스토그램 (percentile 계산용)
    unsigned long long d2c_hist[LAT_HIST_BUCKETS];
    /*
     * D2C 세분화: block_rq_issue -> nvme_complete_rq -> block_rq_complete.
     * 중간 경계 CQ = nvme_complete_rq (NVMe Completion Queue 엔트리 처리 시점).
     * D2CQ(device 왕복) + CQ2C(block 완료) = D2C (놓치는 시간 없음).
     * nvme_complete_rq tracepoint를 받은 I/O만 d2c_traced_count에 센다.
     */
    unsigned long long d2cq_total;  // block_rq_issue   -> nvme_complete_rq (device 왕복)
    unsigned long long cq2c_total;  // nvme_complete_rq -> block_rq_complete (block 완료)
    unsigned long long d2c_traced_count;
};

struct io_stats {
    struct rw_stats stats[IO_MAX_TYPES];
};

/*
 * QD는 device_stats(PERCPU_HASH)에서 분리해 별도의 글로벌 HASH 맵으로 관리한다.
 * block_rq_issue가 SQ CPU에서, block_rq_complete가 CQ(IRQ) CPU에서 실행되므로
 * PERCPU 카운터로는 +1/-1이 서로 다른 CPU에 누적되어 무의미한 값이 나온다.
 * 글로벌 HASH + __sync_fetch_and_add 로 cross-CPU atomic 보장.
 */
struct dev_qd {
    int current_qd[IO_MAX_TYPES];
    unsigned int max_qd[IO_MAX_TYPES];
    /* SQ(issue) CPU와 CQ(complete) CPU 일치/불일치 카운트. cross-CPU atomic. */
    unsigned long long sq_cq_same;
    unsigned long long sq_cq_diff;
    /* device 전체 in-flight QD 분포. block_rq_issue 시점에 sum(current_qd)을 버킷. */
    unsigned long long qd_hist[QD_HIST_BUCKETS];
};

/*
 * 엔진 종류. 트레이서는 libaio·io_uring tracepoint를 항상 동시에 attach하고,
 * 어느 경로가 fire했는지로 I/O별 엔진을 판별한다(사전 mode 지정 없음).
 * engine_stats는 이 인덱스로 엔진마다 분리 누적된다.
 */
enum eng_type {
    ENG_LIBAIO = 0,
    ENG_IOURING,
    ENG_MAX
};

/*
 * 엔진 페이즈(S2Q/C2R/R2U) 누적. block 페이즈(Q2D/D2C) 밖의 구간이다.
 *   S2Q : submit            -> block_bio_queue            (제출 경로)
 *   C2R : block_rq_complete -> aio_complete / io_uring CQE (엔진 완료 핸드오프)
 *   R2U : aio_complete      -> io_getevents 반환           (user 수확)
 * io_uring은 완료를 CQ ring으로 전달(syscall 없음)해 R2U 대응이 없다 → r2u_* = 0.
 * S2Q는 op 구분 없는 글로벌, C2R/R2U는 block 계층에서 분류한 op별.
 * engine_stats_map은 ARRAY[ENG_MAX] — 엔진별로 한 구조체씩.
 */
struct engine_stats {
    unsigned long long s2q_count;
    unsigned long long s2q_lat_total;

    unsigned long long c2r_read_count;
    unsigned long long c2r_read_total;
    unsigned long long c2r_write_count;
    unsigned long long c2r_write_total;
    unsigned long long c2r_flush_count;
    unsigned long long c2r_flush_total;

    unsigned long long r2u_read_count;
    unsigned long long r2u_read_total;
    unsigned long long r2u_write_count;
    unsigned long long r2u_write_total;
    unsigned long long r2u_flush_count;
    unsigned long long r2u_flush_total;
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
