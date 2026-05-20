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

void print_json_report(struct bpf_map *device_stats_map, struct bpf_map *sys_stats_map,
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
                dev_total.stats[t].nvme_total = 0;
                dev_total.stats[t].blkc_total = 0;
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
                        tot_st->nvme_total += cpu_st->nvme_total;
                        tot_st->blkc_total += cpu_st->blkc_total;
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

                        printf("          \"d2c_split\": {\"nvme_total_ns\": %llu, "
                               "\"blkc_total_ns\": %llu, \"traced_count\": %llu}\n",
                               dev_total.stats[t].nvme_total, dev_total.stats[t].blkc_total,
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

    struct libaio_stats sys_st = {0};
    unsigned int sys_key = 0;
    if (sys_stats_map) bpf_map_lookup_elem(bpf_map__fd(sys_stats_map), &sys_key, &sys_st);

    printf("  \"libaio_overhead\": {\n");
    printf("    \"u2q_count\": %llu,\n", sys_st.u2q_count);
    printf("    \"u2q_lat_total\": %llu,\n", sys_st.u2q_lat_total);
    printf("    \"c2a_read_count\": %llu,\n", sys_st.c2a_read_count);
    printf("    \"c2a_read_total\": %llu,\n", sys_st.c2a_read_total);
    printf("    \"c2a_write_count\": %llu,\n", sys_st.c2a_write_count);
    printf("    \"c2a_write_total\": %llu,\n", sys_st.c2a_write_total);
    printf("    \"c2a_flush_count\": %llu,\n", sys_st.c2a_flush_count);
    printf("    \"c2a_flush_total\": %llu,\n", sys_st.c2a_flush_total);
    printf("    \"a2u_read_count\": %llu,\n", sys_st.a2u_read_count);
    printf("    \"a2u_read_total\": %llu,\n", sys_st.a2u_read_total);
    printf("    \"a2u_write_count\": %llu,\n", sys_st.a2u_write_count);
    printf("    \"a2u_write_total\": %llu,\n", sys_st.a2u_write_total);
    printf("    \"a2u_flush_count\": %llu,\n", sys_st.a2u_flush_count);
    printf("    \"a2u_flush_total\": %llu\n", sys_st.a2u_flush_total);
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
    struct bpf_map *sys_stats_map;
    struct bpf_map *device_qd_map;
    struct bpf_map *cpu_matrix_map;

    bool mode_libaio = false;
    double opt_interval = 1.0;   // 초 단위, sub-second (예: 0.5) 허용

    for (int i = 1; i < argc; i++) {
        const char *mode_str = NULL;
        if (strncmp(argv[i], "--mode=", 7) == 0) mode_str = argv[i] + 7;
        else if (strcmp(argv[i], "-m") == 0 && i + 1 < argc) mode_str = argv[++i];

        if (mode_str && strcmp(mode_str, "libaio") == 0) mode_libaio = true;

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

    skel->rodata->opt_trace_libaio = mode_libaio;

    if (!mode_libaio) {
        bpf_program__set_autoattach(skel->progs.trace_submit_enter, false);
        bpf_program__set_autoattach(skel->progs.trace_submit_exit, false);
        bpf_program__set_autoattach(skel->progs.trace_aio_complete, false);
        bpf_program__set_autoattach(skel->progs.trace_getevents_enter, false);
        bpf_program__set_autoattach(skel->progs.trace_getevents_exit, false);
        bpf_program__set_autoattach(skel->progs.trace_pgetevents_enter, false);
        bpf_program__set_autoattach(skel->progs.trace_pgetevents_exit, false);
    }
    
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
    sys_stats_map = bpf_object__find_map_by_name(skel->obj, "sys_stats_map");
    device_qd_map = bpf_object__find_map_by_name(skel->obj, "device_qd");
    cpu_matrix_map = bpf_object__find_map_by_name(skel->obj, "cpu_matrix");

    printf("[PID: %d] io_trace is running (Modes: Generic%s) | Interval: %.3fs\n",
            getpid(), mode_libaio ? " + Libaio" : "", opt_interval);
    fflush(stdout);

    /* opt_interval==0이면 print 비활성 (final report만). 그 외엔 매 tick 0.05초 sleep 후
     * 누적 시간이 opt_interval 넘으면 print. */
    const double tick_s = (opt_interval > 0 && opt_interval < 0.1) ? opt_interval : 0.1;
    struct timespec ts_sleep = { .tv_sec = (time_t)tick_s,
                                 .tv_nsec = (long)((tick_s - (long)tick_s) * 1e9) };
    double elapsed = 0.0;
    while (!stop) {
        nanosleep(&ts_sleep, NULL);
        if (reset_flag) {
            clear_stats_map(bpf_map__fd(device_stats_map));
            if (device_qd_map) clear_stats_map(bpf_map__fd(device_qd_map));
            if (cpu_matrix_map) clear_stats_map(bpf_map__fd(cpu_matrix_map));
            reset_flag = 0;
            elapsed = 0.0;
            continue;
        }
        elapsed += tick_s;

        if (opt_interval > 0 && elapsed + 1e-9 >= opt_interval) {
            print_json_report(device_stats_map, sys_stats_map, device_qd_map,
                              cpu_matrix_map, stats_array, nr_cpus);
            elapsed = 0.0;
        }
    }

    print_json_report(device_stats_map, sys_stats_map, device_qd_map,
                      cpu_matrix_map, stats_array, nr_cpus);

    free(stats_array);
cleanup:
    io_trace_bpf__destroy(skel);
    return 0;
}
