#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <signal.h>
#include <unistd.h>
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

int main(int argc, char **argv) {
    struct io_trace_bpf *skel;
    int err, nr_cpus;
    struct io_stats *stats_array;
    struct bpf_map *device_stats_map;
    struct bpf_map *sys_stats_map;

    const char *type_names[IO_MAX_TYPES] = {"read", "read_ahead", "write", "flush", "discard"};

    // 모드 플래그 (기본: 범용 모드)
    bool mode_libaio = false;
    bool mode_iouring = false; // 향후 확장을 위한 플래그

    // 인자 파싱 (-m <mode> 또는 --mode=<mode>)
    for (int i = 1; i < argc; i++) {
        const char *mode_str = NULL;

        if (strncmp(argv[i], "--mode=", 7) == 0) {
            mode_str = argv[i] + 7;
        } else if (strcmp(argv[i], "-m") == 0 && i + 1 < argc) {
            mode_str = argv[++i];
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            printf("Usage: %s [-m|--mode <mode>]\n", argv[0]);
            printf("  Modes:\n");
            printf("    generic  : Generic Block Trace Mode (U2Q, Q2D, D2C) - Default\n");
            printf("    libaio   : Enable libaio Trace Mode (+ C2A, A2U)\n");
            printf("    iouring  : Enable io_uring Trace Mode (Placeholder)\n");
            return 0;
        } else {
            fprintf(stderr, "Unknown option: %s\n", argv[i]);
            return 1;
        }

        if (mode_str) {
            if (strcmp(mode_str, "libaio") == 0) {
                mode_libaio = true;
            } else if (strcmp(mode_str, "iouring") == 0) {
                mode_iouring = true;
            } else if (strcmp(mode_str, "generic") == 0) {
                // Default mode, no flags to set
            } else {
                fprintf(stderr, "Unknown mode: %s\n", mode_str);
                return 1;
            }
        }
    }

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGUSR1, reset_handler);

    // 1. Open (커널 로드 전)
    skel = io_trace_bpf__open();
    if (!skel) {
        fprintf(stderr, "Failed to open BPF skeleton\n");
        return 1;
    }

    // 2. Global Variable (rodata) 설정
    skel->rodata->opt_trace_libaio = mode_libaio;
    // skel->rodata->opt_trace_iouring = mode_iouring; // 나중에 bpf.c에 추가 시 주석 해제

    // 3. Auto-attach 제어
    if (!mode_libaio) {
        bpf_program__set_autoattach(skel->progs.trace_submit_enter, false);
        bpf_program__set_autoattach(skel->progs.trace_submit_exit, false);
        bpf_program__set_autoattach(skel->progs.trace_aio_complete, false);
        bpf_program__set_autoattach(skel->progs.trace_getevents_enter, false);
        bpf_program__set_autoattach(skel->progs.trace_getevents_exit, false);
        bpf_program__set_autoattach(skel->progs.trace_pgetevents_enter, false);
        bpf_program__set_autoattach(skel->progs.trace_pgetevents_exit, false);
    }
    
    // if (!mode_iouring) {
    //     // io_uring 관련 bpf_program__set_autoattach(..., false) 추가 예정
    // }

    // 4. Load
    err = io_trace_bpf__load(skel);

    // 5. Attach
    err = io_trace_bpf__attach(skel);
    if (err) {
        fprintf(stderr, "Failed to attach BPF skeleton\n");
        goto cleanup;
    }

    nr_cpus = libbpf_num_possible_cpus();
    stats_array = calloc(nr_cpus, sizeof(struct io_stats));
    
    device_stats_map = bpf_object__find_map_by_name(skel->obj, "device_stats");
    sys_stats_map = bpf_object__find_map_by_name(skel->obj, "sys_stats_map");

    printf("[PID: %d] io_trace is running (Modes: Generic%s)...\n", 
           getpid(), mode_libaio ? " + Libaio" : "");

    while (!stop) {
        if (reset_flag) {
            clear_stats_map(bpf_map__fd(device_stats_map));
            reset_flag = 0;
        }
        sleep(1);
    }

    // JSON 출력 로직 (동일)
    printf("\n---JSON_START---\n{\n  \"devices\": [\n");

    unsigned int key = 0, next_key;
    int fd = bpf_map__fd(device_stats_map);
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
                
                for (int b = 0; b < MAX_SIZE_BUCKETS; b++) {
                    dev_total.stats[t].size_hist[b] = 0;
                }
            }

            unsigned long long total_any_io = 0;

            for (int i = 0; i < nr_cpus; i++) {
                for (int t = 0; t < IO_MAX_TYPES; t++) {
                    struct rw_stats *cpu_st = &stats_array[i].stats[t];
                    struct rw_stats *tot_st = &dev_total.stats[t];
                    
                    if (cpu_st->io_count > 0) {
                        tot_st->io_count += cpu_st->io_count;
                        tot_st->total_bytes += cpu_st->total_bytes;
                        
                        for (int b = 0; b < MAX_SIZE_BUCKETS; b++) {
                            tot_st->size_hist[b] += cpu_st->size_hist[b];
                        }
                        
                        tot_st->q2d.total += cpu_st->q2d.total;
                        if (cpu_st->q2d.max > tot_st->q2d.max) tot_st->q2d.max = cpu_st->q2d.max;
                        if (cpu_st->q2d.min < tot_st->q2d.min) tot_st->q2d.min = cpu_st->q2d.min;
                        
                        tot_st->d2c.total += cpu_st->d2c.total;
                        if (cpu_st->d2c.max > tot_st->d2c.max) tot_st->d2c.max = cpu_st->d2c.max;
                        if (cpu_st->d2c.min < tot_st->d2c.min) tot_st->d2c.min = cpu_st->d2c.min;

                        total_any_io += cpu_st->io_count;
                    }
                }
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
                        
                        printf("          \"size_hist\": [%llu, %llu, %llu, %llu],\n", 
                               dev_total.stats[t].size_hist[0],
                               dev_total.stats[t].size_hist[1],
                               dev_total.stats[t].size_hist[2],
                               dev_total.stats[t].size_hist[3]);
                        
                        printf("          \"q2d\": {\n");
                        printf("            \"total_lat_ns\": %llu,\n", dev_total.stats[t].q2d.total);
                        printf("            \"min_lat_ns\": %llu,\n", dev_total.stats[t].q2d.min == (unsigned long long)-1 ? 0 : dev_total.stats[t].q2d.min);
                        printf("            \"max_lat_ns\": %llu\n", dev_total.stats[t].q2d.max);
                        printf("          },\n");
                        
                        printf("          \"d2c\": {\n");
                        printf("            \"total_lat_ns\": %llu,\n", dev_total.stats[t].d2c.total);
                        printf("            \"min_lat_ns\": %llu,\n", dev_total.stats[t].d2c.min == (unsigned long long)-1 ? 0 : dev_total.stats[t].d2c.min);
                        printf("            \"max_lat_ns\": %llu\n", dev_total.stats[t].d2c.max);
                        printf("          }\n");
                        printf("        }");
                        first_op = 0;
                    }
                }
                printf("\n      }\n    }");
                first_dev = 0;
            }
        }
        key = next_key;
    }

    printf("\n  ],\n");

    struct libaio_stats sys_st = {0};
    unsigned int sys_key = 0;
    
    if (sys_stats_map) {
        bpf_map_lookup_elem(bpf_map__fd(sys_stats_map), &sys_key, &sys_st);
    }

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
    printf("  }\n");

    printf("}\n---JSON_END---\n");

    free(stats_array);
cleanup:
    io_trace_bpf__destroy(skel);
    return 0;
}
