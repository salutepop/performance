"""scenario: PCIe contention — GPU host↔device memcpy 부하 + 동시 fio randread.

목적: 디스크 워크로드와 GPU bus 트래픽이 같은 PCIe lane을 공유하는 환경에서
경합 발생 시 fio bandwidth/latency가 받는 영향 측정.

주: NVLink-C2C로 host-GPU가 연결되는 시스템(GB10 등)에선 cudaMemcpy가
PCIe 트래픽이 아니므로 의미 없음 — 그 경우 gpu_pcie_{rx,tx}_mb_s가 0 가까이
유지되는 것 자체가 결과 (uniform memory 검증).

실행:
  python3 -m scenarios.pcie_contention
"""

import os
import subprocess
import sys
import shutil
import tempfile
import time

from .base import Scenario, ROOT


_CUDA_SRC = r'''
#include <cuda_runtime.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
int main(int argc, char **argv) {
    size_t N = 256ULL << 20;  // 256MB bounce
    void *d = NULL, *h = NULL;
    cudaError_t err = cudaMalloc(&d, N);
    if (err != cudaSuccess) { fprintf(stderr, "cudaMalloc: %s\n", cudaGetErrorString(err)); return 1; }
    err = cudaMallocHost(&h, N);
    if (err != cudaSuccess) { fprintf(stderr, "cudaMallocHost: %s\n", cudaGetErrorString(err)); return 1; }
    int sec = argc > 1 ? atoi(argv[1]) : 8;
    time_t start = time(NULL);
    unsigned long iters = 0;
    while (time(NULL) - start < sec) {
        cudaMemcpy(d, h, N, cudaMemcpyHostToDevice);
        cudaMemcpy(h, d, N, cudaMemcpyDeviceToHost);
        iters++;
    }
    fprintf(stderr, "pcie_load: %lu iterations (%zu MB each direction)\n", iters, N >> 20);
    cudaFree(d);
    cudaFreeHost(h);
    return 0;
}
'''


def _build_loader():
    """nvcc로 PCIe loader 컴파일. 실패하면 None 반환 → scenario는 GPU 부하 없이 진행."""
    if not shutil.which("nvcc"):
        return None
    bin_path = "/tmp/pcie_contention_loader"
    if os.path.exists(bin_path):
        return bin_path
    with tempfile.NamedTemporaryFile(suffix=".cu", delete=False, mode="w") as f:
        f.write(_CUDA_SRC)
        src_path = f.name
    try:
        rc = subprocess.call(["nvcc", "-O2", "-o", bin_path, src_path],
                              stderr=subprocess.STDOUT, stdout=subprocess.DEVNULL)
        if rc != 0 or not os.path.exists(bin_path):
            return None
        return bin_path
    except Exception:
        return None
    finally:
        try: os.unlink(src_path)
        except Exception: pass


class PcieContention(Scenario):
    name = "pcie_contention"
    mode = "libaio"
    interval = 1.0
    load_seconds = 8     # GPU bounce duration. fio runtime과 비슷하게 잡음.

    def fio_cmd(self):
        return (
            f"fio --name=pcie_test --filename=/tmp/fio_smoke.dat "
            f"--rw=randread --bs=4k --iodepth=32 --size=128M "
            f"--runtime={self.load_seconds - 1} --time_based --direct=1 --ioengine=libaio "
            f"--numjobs=2 --group_reporting"
        )

    def run(self):
        loader = _build_loader()
        loader_proc = None
        if loader:
            print(f"[scenario {self.name}] launching GPU PCIe loader ({self.load_seconds}s)...")
            loader_proc = subprocess.Popen([loader, str(self.load_seconds)],
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE)
            time.sleep(0.5)  # GPU 메모리 할당 잠깐 대기
        else:
            print(f"[scenario {self.name}] WARN: nvcc 미사용/컴파일 실패 — fio 단독 측정")
        rc = super().run()
        if loader_proc:
            try:
                loader_proc.wait(timeout=self.load_seconds + 5)
                stderr = loader_proc.stderr.read().decode("utf-8", "replace")
                if stderr:
                    print(f"[scenario {self.name}] loader stderr: {stderr.strip()}")
            except subprocess.TimeoutExpired:
                loader_proc.kill()
        return rc

    def analyze(self, summary):
        devs = summary.get("devices") or {}
        if not devs:
            return {"pass": False, "reason": "no devices"}
        dname = next(iter(devs))
        read = devs[dname]["ops"].get("read") or {}
        gpu = (summary.get("system") or {}).get("gpu") or {}
        gpu0 = next(iter(gpu.values())) if gpu else {}
        bw = read.get("bw_mb_avg")
        d2c = read.get("d2c_us_avg")
        gpu_peak_pwr = gpu0.get("power_w_peak")
        return {
            "fio_bw_avg_mb_s": bw,
            "fio_d2c_avg_us": d2c,
            "gpu_power_w_peak": gpu_peak_pwr,
            "gpu_present": bool(gpu),
            "pass": bw is not None and d2c is not None,
        }


if __name__ == "__main__":
    sys.exit(PcieContention().run())
