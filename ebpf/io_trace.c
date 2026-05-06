#include <stdio.h>
#include <stdlib.h>
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

    // JSON 생성을 위한 타입명 매핑
    const char *type_names[IO_MAX_TYPES] = {
        "read", "read_ahead", "write", "flush", "discard"
    };

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGUSR1, reset_handler);

    skel = io_trace_bpf__open_and_load();
    if (!skel) return 1;

    err = io_trace_bpf__attach(skel);
    if (err) goto cleanup;

    nr_cpus = libbpf_num_possible_cpus();
    stats_array = calloc(nr_cpus, sizeof(struct io_stats));

    printf("[PID: %d] io_trace is running (All Operations Tracking Mode)...\n", getpid());

    while (!stop) {
        if (reset_flag) {
            clear_stats_map(bpf_map__fd(skel->maps.device_stats));
            reset_flag = 0;
        }
        sleep(1);
    }

    printf("\n---JSON_START---\n{\n  \"devices\": [\n");

    unsigned int key = 0, next_key;
    int fd = bpf_map__fd(skel->maps.device_stats);
    int first_dev = 1;

    while (bpf_map_get_next_key(fd, &key, &next_key) == 0) {
        if (bpf_map_lookup_elem(fd, &next_key, stats_array) == 0) {
            
            struct io_stats dev_total;
            for (int t = 0; t < IO_MAX_TYPES; t++) {
                dev_total.stats[t].io_count = 0;
                dev_total.stats[t].total_latency = 0;
                dev_total.stats[t].total_bytes = 0;
                dev_total.stats[t].max_latency = 0;
                dev_total.stats[t].min_latency = (unsigned long long)-1;
            }

            unsigned long long total_any_io = 0;

            for (int i = 0; i < nr_cpus; i++) {
                for (int t = 0; t < IO_MAX_TYPES; t++) {
                    if (stats_array[i].stats[t].io_count > 0) {
                        dev_total.stats[t].io_count += stats_array[i].stats[t].io_count;
                        dev_total.stats[t].total_latency += stats_array[i].stats[t].total_latency;
                        dev_total.stats[t].total_bytes += stats_array[i].stats[t].total_bytes;
                        if (stats_array[i].stats[t].max_latency > dev_total.stats[t].max_latency) 
                            dev_total.stats[t].max_latency = stats_array[i].stats[t].max_latency;
                        if (stats_array[i].stats[t].min_latency < dev_total.stats[t].min_latency) 
                            dev_total.stats[t].min_latency = stats_array[i].stats[t].min_latency;
                        total_any_io += stats_array[i].stats[t].io_count;
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
                        printf("          \"total_lat_ns\": %llu,\n", dev_total.stats[t].total_latency);
                        printf("          \"min_lat_ns\": %llu,\n", dev_total.stats[t].min_latency);
                        printf("          \"max_lat_ns\": %llu\n", dev_total.stats[t].max_latency);
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

    printf("\n  ]\n}\n---JSON_END---\n");

    free(stats_array);
cleanup:
    io_trace_bpf__destroy(skel);
    return 0;
}
