#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include <dirent.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include "io_trace.h"
#include "io_trace.skel.h"

static volatile sig_atomic_t stop = 0;
static volatile sig_atomic_t reset_flag = 0;

void sig_handler(int sig) { stop = 1; }
void reset_handler(int sig) { reset_flag = 1; }

void clear_stats_map(int fd) {
    unsigned int key = 0, next_key;
    while (bpf_map_get_next_key(fd, &key, &next_key) == 0) {
        bpf_map_delete_elem(fd, &next_key);
        key = next_key;
    }
}

/*
 * /proc/kallsyms에서 커널 심볼의 런타임 주소를 읽는다. static 함수도 포함된다
 * (kallsyms는 전역 'T'와 로컬 't'를 모두 노출). io_trace는 root로 실행되므로
 * kptr_restrict와 무관하게 KASLR이 적용된 실제 주소가 보인다. 못 찾으면 0.
 */
static unsigned long long resolve_ksym(const char *name) {
    FILE *f = fopen("/proc/kallsyms", "r");
    if (!f) return 0;
    char line[512], sym[256], type;
    unsigned long long addr, found = 0;
    while (fgets(line, sizeof(line), f)) {
        if (sscanf(line, "%llx %c %255s", &addr, &type, sym) == 3
            && strcmp(sym, name) == 0) {
            found = addr;
            break;
        }
    }
    fclose(f);
    return found;
}

/* engine_overhead JSON 한 엔진 블록 출력. last면 trailing comma 생략. */
static void print_engine_json(const char *name, const struct engine_stats *e, int last) {
    printf("    \"%s\": {\n", name);
    printf("      \"s2q_count\": %llu,\n", e->s2q_count);
    printf("      \"s2q_lat_total\": %llu,\n", e->s2q_lat_total);
    printf("      \"c2r_read_count\": %llu,\n", e->c2r_read_count);
    printf("      \"c2r_read_total\": %llu,\n", e->c2r_read_total);
    printf("      \"c2r_write_count\": %llu,\n", e->c2r_write_count);
    printf("      \"c2r_write_total\": %llu,\n", e->c2r_write_total);
    printf("      \"c2r_flush_count\": %llu,\n", e->c2r_flush_count);
    printf("      \"c2r_flush_total\": %llu,\n", e->c2r_flush_total);
    printf("      \"r2u_read_count\": %llu,\n", e->r2u_read_count);
    printf("      \"r2u_read_total\": %llu,\n", e->r2u_read_total);
    printf("      \"r2u_write_count\": %llu,\n", e->r2u_write_count);
    printf("      \"r2u_write_total\": %llu,\n", e->r2u_write_total);
    printf("      \"r2u_flush_count\": %llu,\n", e->r2u_flush_count);
    printf("      \"r2u_flush_total\": %llu\n", e->r2u_flush_total);
    printf("    }%s\n", last ? "" : ",");
}

void print_json_report(struct bpf_map *device_stats_map, struct bpf_map *engine_stats_map,
                       struct bpf_map *device_qd_map, struct bpf_map *cpu_matrix_map,
                       struct io_stats *stats_array, int nr_cpus) {
    const char *type_names[IO_MAX_TYPES] = {"read", "read_ahead", "write", "flush", "discard"};

    printf("\n---JSON_START---\n{\n  \"devices\": [\n");

    unsigned int key = 0, next_key;
    int fd = bpf_map__fd(device_stats_map);
    int qd_fd = device_qd_map ? bpf_map__fd(device_qd_map) : -1;
    int first_dev = 1;

    while (bpf_map_get_next_key(fd, &key, &next_key) == 0) {
        if (bpf_map_lookup_elem(fd, &next_key, stats_array) == 0) {
            struct io_stats dev_total;
            for (int t = 0; t < IO_MAX_TYPES; t++) {
                dev_total.stats[t].io_count = 0;
                dev_total.stats[t].total_bytes = 0;
                dev_total.stats[t].q2d.total = 0;
                dev_total.stats[t].q2d.max = 0;
                dev_total.stats[t].q2d.min = (unsigned long long)-1;
                dev_total.stats[t].d2c.total = 0;
                dev_total.stats[t].d2c.max = 0;
                dev_total.stats[t].d2c.min = (unsigned long long)-1;
                for (int b = 0; b < MAX_SIZE_BUCKETS; b++) dev_total.stats[t].size_hist[b] = 0;
                for (int b = 0; b < LBA_BUCKETS; b++) dev_total.stats[t].lba_hist[b] = 0;
                for (int b = 0; b < LAT_HIST_BUCKETS; b++) {
                    dev_total.stats[t].q2d_hist[b] = 0;
                    dev_total.stats[t].d2c_hist[b] = 0;
                }
                dev_total.stats[t].d2cq_total = 0;
                dev_total.stats[t].cq2c_total = 0;
                dev_total.stats[t].d2c_traced_count = 0;
            }

            unsigned long long total_any_io = 0;
            for (int i = 0; i < nr_cpus; i++) {
                for (int t = 0; t < IO_MAX_TYPES; t++) {
                    struct rw_stats *cpu_st = &stats_array[i].stats[t];
                    struct rw_stats *tot_st = &dev_total.stats[t];
                    if (cpu_st->io_count > 0) {
                        tot_st->io_count += cpu_st->io_count;
                        tot_st->total_bytes += cpu_st->total_bytes;

                        for (int b = 0; b < MAX_SIZE_BUCKETS; b++) tot_st->size_hist[b] += cpu_st->size_hist[b];
                        for (int b = 0; b < LBA_BUCKETS; b++) tot_st->lba_hist[b] += cpu_st->lba_hist[b];
                        for (int b = 0; b < LAT_HIST_BUCKETS; b++) {
                            tot_st->q2d_hist[b] += cpu_st->q2d_hist[b];
                            tot_st->d2c_hist[b] += cpu_st->d2c_hist[b];
                        }
                        tot_st->q2d.total += cpu_st->q2d.total;
                        if (cpu_st->q2d.max > tot_st->q2d.max) tot_st->q2d.max = cpu_st->q2d.max;
                        if (cpu_st->q2d.min < tot_st->q2d.min) tot_st->q2d.min = cpu_st->q2d.min;
                        tot_st->d2c.total += cpu_st->d2c.total;
                        if (cpu_st->d2c.max > tot_st->d2c.max) tot_st->d2c.max = cpu_st->d2c.max;
                        if (cpu_st->d2c.min < tot_st->d2c.min) tot_st->d2c.min = cpu_st->d2c.min;
                        tot_st->d2cq_total += cpu_st->d2cq_total;
                        tot_st->cq2c_total += cpu_st->cq2c_total;
                        tot_st->d2c_traced_count += cpu_st->d2c_traced_count;
                        total_any_io += cpu_st->io_count;
                    }
                }
            }

            // QD는 별도 글로벌 HASH 맵에서 가져온다 (cross-CPU atomic 기반).
            struct dev_qd qd_data = {0};
            if (qd_fd >= 0) {
                bpf_map_lookup_elem(qd_fd, &next_key, &qd_data);
            }

            if (total_any_io > 0) {
                if (!first_dev) printf(",\n");
                unsigned int major = next_key >> 20;
                unsigned int minor = next_key & 0xFFFFF;

                printf("    {\n");
                printf("      \"dev_name\": \"dev(%u:%u)\",\n", major, minor);
                printf("      \"operations\": {\n");

                int first_op = 1;
                for (int t = 0; t < IO_MAX_TYPES; t++) {
                    if (dev_total.stats[t].io_count > 0) {
                        if (!first_op) printf(",\n");
                        printf("        \"%s\": {\n", type_names[t]);
                        printf("          \"total_count\": %llu,\n", dev_total.stats[t].io_count);
                        printf("          \"total_bytes\": %llu,\n", dev_total.stats[t].total_bytes);
                        printf("          \"current_qd\": %d,\n", qd_data.current_qd[t]);
                        printf("          \"max_qd\": %u,\n", qd_data.max_qd[t]);
                        
                        printf("          \"size_hist\": [%llu, %llu, %llu, %llu],\n", 
                               dev_total.stats[t].size_hist[0], dev_total.stats[t].size_hist[1],
                               dev_total.stats[t].size_hist[2], dev_total.stats[t].size_hist[3]);
                        
                        printf("          \"lba_hist\": [");
                        for (int b = 0; b < LBA_BUCKETS; b++) {
                            printf("%u%s", dev_total.stats[t].lba_hist[b], (b == LBA_BUCKETS - 1) ? "" : ",");
                        }
                        printf("],\n");
                        
                        printf("          \"q2d\": {\n");
                        printf("            \"total_lat_ns\": %llu,\n", dev_total.stats[t].q2d.total);
                        printf("            \"min_lat_ns\": %llu,\n", dev_total.stats[t].q2d.min == (unsigned long long)-1 ? 0 : dev_total.stats[t].q2d.min);
                        printf("            \"max_lat_ns\": %llu\n", dev_total.stats[t].q2d.max);
                        printf("          },\n");
                        
                        printf("          \"d2c\": {\n");
                        printf("            \"total_lat_ns\": %llu,\n", dev_total.stats[t].d2c.total);
                        printf("            \"min_lat_ns\": %llu,\n", dev_total.stats[t].d2c.min == (unsigned long long)-1 ? 0 : dev_total.stats[t].d2c.min);
                        printf("            \"max_lat_ns\": %llu\n", dev_total.stats[t].d2c.max);
                        printf("          },\n");

                        printf("          \"q2d_hist\": [");
                        for (int b = 0; b < LAT_HIST_BUCKETS; b++) {
                            printf("%llu%s", dev_total.stats[t].q2d_hist[b], (b == LAT_HIST_BUCKETS - 1) ? "" : ",");
                        }
                        printf("],\n");

                        printf("          \"d2c_hist\": [");
                        for (int b = 0; b < LAT_HIST_BUCKETS; b++) {
                            printf("%llu%s", dev_total.stats[t].d2c_hist[b], (b == LAT_HIST_BUCKETS - 1) ? "" : ",");
                        }
                        printf("],\n");

                        printf("          \"d2c_split\": {\"d2cq_total_ns\": %llu, "
                               "\"cq2c_total_ns\": %llu, \"traced_count\": %llu}\n",
                               dev_total.stats[t].d2cq_total, dev_total.stats[t].cq2c_total,
                               dev_total.stats[t].d2c_traced_count);
                        printf("        }");
                        first_op = 0;
                    }
                }
                printf("\n      },\n");
                printf("      \"sqcq\": {\"same\": %llu, \"diff\": %llu},\n",
                       qd_data.sq_cq_same, qd_data.sq_cq_diff);
                printf("      \"qd_hist\": [");
                for (int b = 0; b < QD_HIST_BUCKETS; b++) {
                    printf("%llu%s", qd_data.qd_hist[b], (b == QD_HIST_BUCKETS - 1) ? "" : ",");
                }
                printf("]\n");
                printf("    }");
                first_dev = 0;
            }
        }
        key = next_key;
    }
    printf("\n  ],\n");

    /* 엔진 페이즈(S2Q/C2R/R2U) — 엔진별 분리 출력. engine_stats_map은 ARRAY[ENG_MAX].
     * 두 엔진 tracepoint가 항상 attach되므로, 비활성 엔진은 전부 0으로 나온다. */
    struct engine_stats eng_libaio = {0}, eng_iouring = {0};
    if (engine_stats_map) {
        int ef = bpf_map__fd(engine_stats_map);
        unsigned int k_lib = ENG_LIBAIO, k_iou = ENG_IOURING;
        bpf_map_lookup_elem(ef, &k_lib, &eng_libaio);
        bpf_map_lookup_elem(ef, &k_iou, &eng_iouring);
    }
    printf("  \"engine_overhead\": {\n");
    print_engine_json("libaio", &eng_libaio, 0);
    print_engine_json("iouring", &eng_iouring, 1);
    printf("  },\n");

    /* SQ x CQ CPU 매트릭스 — sparse: 실제 발생한 (issue,cq) 쌍만. */
    printf("  \"sqcq_matrix\": [");
    if (cpu_matrix_map) {
        int m_fd = bpf_map__fd(cpu_matrix_map);
        unsigned int mk = 0, mnext;
        int first_m = 1;
        while (bpf_map_get_next_key(m_fd, &mk, &mnext) == 0) {
            unsigned long long cnt = 0;
            if (bpf_map_lookup_elem(m_fd, &mnext, &cnt) == 0 && cnt > 0) {
                unsigned int issue_cpu = mnext >> 16;
                unsigned int cq_cpu = mnext & 0xFFFF;
                printf("%s[%u,%u,%llu]", first_m ? "" : ",", issue_cpu, cq_cpu, cnt);
                first_m = 0;
            }
            mk = mnext;
        }
    }
    printf("]\n");
    printf("}\n---JSON_END---\n");

    fflush(stdout);
}

int main(int argc, char **argv) {
    struct io_trace_bpf *skel;
    int err, nr_cpus;
    struct io_stats *stats_array;
    struct bpf_map *device_stats_map;
    struct bpf_map *engine_stats_map;
    struct bpf_map *device_qd_map;
    struct bpf_map *cpu_matrix_map;

    double opt_interval = 1.0;   // 초 단위, sub-second (예: 0.5) 허용

    /* 엔진(libaio/io_uring) 사전 지정 없음 — 두 엔진 tracepoint를 항상 attach하고
     * 어느 게 fire했는지로 I/O별 엔진을 판별한다. -m 인자는 받아도 무시(하위호환). */
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "--interval=", 11) == 0) {
            opt_interval = atof(argv[i] + 11);
        } else if (strcmp(argv[i], "-i") == 0 && i + 1 < argc) {
            opt_interval = atof(argv[++i]);
        }
    }
    /* 너무 짧으면 BPF map clear와 print 부하로 도구가 자체로 노이즈가 됨. 최소 50ms 강제. */
    if (opt_interval > 0 && opt_interval < 0.05) opt_interval = 0.05;

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGUSR1, reset_handler);

    skel = io_trace_bpf__open();
    if (!skel) return 1;

    /* C2R 경로 판별용 dio 완료 콜백 주소 — block_rq_complete의 bio->bi_end_io와
     * 비교해 파일(iomap) / raw blockdev 경로를 가른다. 심볼을 못 찾으면 해당
     * 경로의 C2R만 누락되고 나머지는 정상. 모든 엔진 tracepoint가 상시 attach. */
    skel->rodata->addr_iomap_dio_end_io    = resolve_ksym("iomap_dio_bio_end_io");
    skel->rodata->addr_blkdev_end_io       = resolve_ksym("blkdev_bio_end_io");
    skel->rodata->addr_blkdev_end_io_async = resolve_ksym("blkdev_bio_end_io_async");
    if (!skel->rodata->addr_iomap_dio_end_io)
        fprintf(stderr, "[io_trace] warn: iomap_dio_bio_end_io not in kallsyms"
                        " — file(ext4) C2R unavailable\n");
    if (!skel->rodata->addr_blkdev_end_io && !skel->rodata->addr_blkdev_end_io_async)
        fprintf(stderr, "[io_trace] warn: blkdev_bio_end_io* not in kallsyms"
                        " — raw block device C2R unavailable\n");

    err = io_trace_bpf__load(skel);
    if (err) goto cleanup;

    DIR *d = opendir("/sys/dev/block");
    if (d) {
        struct dirent *dir;
        int cap_fd = bpf_map__fd(skel->maps.dev_capacity_map);
        while ((dir = readdir(d)) != NULL) {
            unsigned int maj, min;
            if (sscanf(dir->d_name, "%u:%u", &maj, &min) == 2) {
                char path[512];
                snprintf(path, sizeof(path), "/sys/dev/block/%s/size", dir->d_name);
                FILE *f = fopen(path, "r");
                if (f) {
                    unsigned long long size_sectors;
                    if (fscanf(f, "%llu", &size_sectors) == 1) {
                        unsigned int dev_key = (maj << 20) | min;
                        bpf_map_update_elem(cap_fd, &dev_key, &size_sectors, BPF_ANY);
                    }
                    fclose(f);
                }
            }
        }
        closedir(d);
    }

    err = io_trace_bpf__attach(skel);
    if (err) goto cleanup;

    nr_cpus = libbpf_num_possible_cpus();
    stats_array = calloc(nr_cpus, sizeof(struct io_stats));
    device_stats_map = bpf_object__find_map_by_name(skel->obj, "device_stats");
    engine_stats_map = bpf_object__find_map_by_name(skel->obj, "engine_stats_map");
    device_qd_map = bpf_object__find_map_by_name(skel->obj, "device_qd");
    cpu_matrix_map = bpf_object__find_map_by_name(skel->obj, "cpu_matrix");

    printf("[PID: %d] io_trace is running (auto-detect: block + libaio + io_uring)"
           " | Interval: %.3fs\n", getpid(), opt_interval);
    fflush(stdout);

    /*
     * 리포트 cadence는 절대 시각(CLOCK_MONOTONIC) deadline으로 잡는다.
     * 과거엔 tick마다 elapsed += tick_s 를 누적해 opt_interval을 넘으면 print했는데,
     * nanosleep 오버슬립 + print_json_report 소요 시간이 매 인터벌 누적돼 리포트
     * cadence가 wall-clock보다 느리게 드리프트했다 — 15초 실행에서 리포트가 한 개
     * 누락돼 device CSV가 wall-clock 1초를 통째로 건너뛰었고, 리포트 차트가
     * 그 누락된 초를 master timeline(SystemMonitor, 드리프트 없음)에 reindex하며
     * D2C/Q2D 라인이 워크로드 도중에 끊겨 보였다. deadline += opt_interval 은
     * 처리 시간/jitter를 누적하지 않아 SystemMonitor._poll_loop와 같은 1Hz 격자에
     * 정렬된다. tick(<=0.1s)은 SIGINT/SIGUSR1 응답성 유지용으로만 남긴다.
     */
    const double tick_s = (opt_interval > 0 && opt_interval < 0.1) ? opt_interval : 0.1;
    struct timespec ts_sleep = { .tv_sec = (time_t)tick_s,
                                 .tv_nsec = (long)((tick_s - (long)tick_s) * 1e9) };
    struct timespec ts_now;
    clock_gettime(CLOCK_MONOTONIC, &ts_now);
    double next_report = ts_now.tv_sec + ts_now.tv_nsec / 1e9 + opt_interval;
    while (!stop) {
        nanosleep(&ts_sleep, NULL);
        if (reset_flag) {
            clear_stats_map(bpf_map__fd(device_stats_map));
            if (device_qd_map) clear_stats_map(bpf_map__fd(device_qd_map));
            if (cpu_matrix_map) clear_stats_map(bpf_map__fd(cpu_matrix_map));
            reset_flag = 0;
            clock_gettime(CLOCK_MONOTONIC, &ts_now);
            next_report = ts_now.tv_sec + ts_now.tv_nsec / 1e9 + opt_interval;
            continue;
        }
        if (opt_interval <= 0)
            continue;

        clock_gettime(CLOCK_MONOTONIC, &ts_now);
        double now_s = ts_now.tv_sec + ts_now.tv_nsec / 1e9;
        if (now_s + 1e-9 >= next_report) {
            print_json_report(device_stats_map, engine_stats_map,
                              device_qd_map, cpu_matrix_map, stats_array, nr_cpus);
            next_report += opt_interval;
            /* print이 한 인터벌 넘게 걸려 deadline이 과거가 됐으면, 밀린 만큼
             * 리포트를 몰아 찍지 말고 현재 시각 기준으로 다음 격자에 재동기화. */
            if (next_report <= now_s)
                next_report = now_s + opt_interval;
        }
    }

    print_json_report(device_stats_map, engine_stats_map,
                      device_qd_map, cpu_matrix_map, stats_array, nr_cpus);

    free(stats_array);
cleanup:
    io_trace_bpf__destroy(skel);
    return 0;
}
