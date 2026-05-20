"""eBPF block-layer I/O tracer collector.

collector.py is the standalone Python orchestrator that drives the native
io_trace binary built from src/. EbpfIoCollector adapts it to the Collector
lifecycle so a Session can run it alongside other collectors.

See CLAUDE.md for the full architecture (Q2D/D2C/U2Q/C2A/A2U phases, BPF maps,
the JSON contract between layers).

Build:
    make -C monitoring/collectors/ebpf_io/src
"""

import os
import signal
import subprocess
import sys
import time

from ..base import Collector

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
IO_TRACE_BIN = os.path.join(_PKG_DIR, "src", "io_trace")
COLLECTOR_PY = os.path.join(_PKG_DIR, "collector.py")
BUILD_HINT = "make -C monitoring/collectors/ebpf_io/src"


def ebpf_available():
    """True if the io_trace binary + collector.py wrapper are both present."""
    return (
        os.path.isfile(IO_TRACE_BIN)
        and os.access(IO_TRACE_BIN, os.X_OK)
        and os.path.isfile(COLLECTOR_PY)
    )


class EbpfIoCollector(Collector):
    """Runs the eBPF I/O tracer (collector.py) as a child process.

    The child is launched with --no-sysmon: the Session owns SystemMonitor,
    so the eBPF collector only does block-layer tracing here.
    """

    name = "ebpf_io"

    def __init__(self, mode="libaio", interval=1.0, verbose=True):
        self.mode = mode
        self.interval = float(interval)
        self.verbose = verbose
        self._proc = None

    def start(self, session_dir, session_id, sys_info):
        if not ebpf_available():
            self._warn(f"io_trace binary missing — skipping (build with `{BUILD_HINT}`)")
            return
        cmd = [
            sys.executable, COLLECTOR_PY,
            "-m", self.mode,
            "-i", str(self.interval),
            "--output-dir", session_dir,
            "--session-id", session_id,
            "--no-sysmon",  # Session owns SystemMonitor; avoid a duplicate
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:
            self._warn(f"tracer start failed: {e}")
            return
        # collector.py sleeps ~1.5s before SIGUSR1 reset; wait for attach to settle.
        time.sleep(2.5)
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            self._warn(f"tracer exited immediately (rc={proc.returncode}): {err.strip()[:300]}")
            return
        self._proc = proc
        if self.verbose:
            print(f"  [eBPF] tracer started (mode={self.mode}, "
                  f"interval={self.interval}s) -> {session_dir}")

    def stop(self):
        proc = self._proc
        if not proc:
            return
        try:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._warn("tracer not responding - sending SIGTERM")
                proc.terminate()
                proc.wait(timeout=5)
            if self.verbose:
                print(f"  [eBPF] tracer stopped (rc={proc.returncode})")
        except Exception as e:
            self._warn(f"tracer stop error: {e}")
        finally:
            self._proc = None
