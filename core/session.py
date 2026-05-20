"""
Session — monitoring + (optional) eBPF tracer + report generation lifecycle.

A Session is the unit of observation: a directory on disk plus the collectors
running into it. Workloads (fio, scripts, TC scenarios) run *inside* a Session
context; the Session itself doesn't care what runs, only that the observation
window is correctly opened and closed.

Usage:
    with Session(session_dir, sys_info, ebpf_mode="libaio", reports="all") as s:
        run_workload(...)            # anything that issues I/O
    # on exit: ebpf stop -> monitor stop -> reports generated

Collectors started:
    - SystemMonitor (always; /proc + /sys + nvidia-smi polling)
    - eBPF I/O tracer (if ebpf_mode != "off" and binary available)

After exit, the configured report formats are rendered in-place against the
session directory.
"""

import os
import signal
import subprocess
import sys
import time

from .monitor import SystemMonitor


_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IO_TRACE_BIN = os.path.join(_PROJ_ROOT, "ebpf", "io_trace")
_IO_PROFILER_PY = os.path.join(_PROJ_ROOT, "ebpf", "io_profiler.py")


def ebpf_available():
    """True if the io_trace binary + io_profiler.py wrapper are both present."""
    return (
        os.path.isfile(_IO_TRACE_BIN)
        and os.access(_IO_TRACE_BIN, os.X_OK)
        and os.path.isfile(_IO_PROFILER_PY)
    )


def resolve_ebpf_mode(toggle, mode):
    """Resolve --ebpf {auto,on,off} + --ebpf-mode into a concrete mode string.

    Returns: "off" | "generic" | "libaio" | "iouring".
    Exits with rc=1 if toggle="on" but binary missing.
    """
    if toggle == "off":
        return "off"
    if toggle == "on":
        if not ebpf_available():
            print(
                "[Error] --ebpf on but ebpf/io_trace binary missing or not executable. "
                "Run `cd ebpf && make` first.",
                file=sys.stderr,
            )
            sys.exit(1)
        return mode
    # auto
    return mode if ebpf_available() else "off"


class Session:
    """Context manager that runs collectors for the lifetime of a workload.

    Parameters
    ----------
    session_dir : str
        Absolute path to the session output directory. Created if missing.
    sys_info : dict
        Discovered system info (passed to SystemMonitor; included in metadata).
    ebpf_mode : str
        "off" disables the eBPF tracer. Otherwise one of generic/libaio/iouring.
    ebpf_interval : float
        eBPF timeseries CSV polling interval (s). 0 disables timeseries.
    monitor_interval : float
        SystemMonitor polling interval (s).
    reports : str
        "none" or a comma-list of html,md,json,png,pdf, or "all".
    verbose : bool
        If True, prints lifecycle messages.
    """

    def __init__(
        self,
        session_dir,
        sys_info,
        ebpf_mode="off",
        ebpf_interval=1.0,
        monitor_interval=1.0,
        reports="none",
        verbose=True,
    ):
        self.session_dir = session_dir
        self.sys_info = sys_info
        self.ebpf_mode = ebpf_mode
        self.ebpf_interval = float(ebpf_interval)
        self.monitor_interval = float(monitor_interval)
        self.reports = reports or "none"
        self.verbose = verbose

        self._monitor = None
        self._ebpf_proc = None
        os.makedirs(self.session_dir, exist_ok=True)

    @property
    def session_id(self):
        return os.path.basename(self.session_dir.rstrip(os.sep))

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self):
        self._start_monitor()
        if self.ebpf_mode != "off":
            self._start_ebpf()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_ebpf()
        self._stop_monitor()
        self._run_reports()
        return False  # never suppress exceptions

    # ------------------------------------------------------------------ collectors
    def _start_monitor(self):
        try:
            self._monitor = SystemMonitor(
                self.session_dir,
                session_id=self.session_id,
                interval=self.monitor_interval,
                sys_info=self.sys_info,
            )
            self._monitor.start()
        except Exception as e:
            self._monitor = None
            print(
                f"  [!] SystemMonitor start failed (reports will use fio JSON only): {e}",
                file=sys.stderr,
            )

    def _stop_monitor(self):
        if not self._monitor:
            return
        try:
            self._monitor.stop()
        except Exception as e:
            print(f"  [!] SystemMonitor stop error: {e}", file=sys.stderr)
        finally:
            self._monitor = None

    def _start_ebpf(self):
        if not ebpf_available():
            print(
                "  [!] eBPF requested but io_trace binary missing — skipping",
                file=sys.stderr,
            )
            return
        cmd = [
            sys.executable,
            _IO_PROFILER_PY,
            "-m",
            self.ebpf_mode,
            "-i",
            str(self.ebpf_interval),
            "--output-dir",
            self.session_dir,
            "--session-id",
            self.session_id,
            "--no-sysmon",  # Session owns SystemMonitor; avoid duplicate
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
        except Exception as e:
            print(f"  [!] eBPF tracer start failed: {e}", file=sys.stderr)
            return
        # io_profiler.py sleeps ~1.5s before SIGUSR1 reset; wait for attach to settle.
        time.sleep(2.5)
        if proc.poll() is not None:
            err = (
                proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            )
            print(
                f"  [!] eBPF tracer exited immediately (rc={proc.returncode}): "
                f"{err.strip()[:300]}",
                file=sys.stderr,
            )
            return
        self._ebpf_proc = proc
        if self.verbose:
            print(
                f"  [eBPF] tracer started (mode={self.ebpf_mode}, "
                f"interval={self.ebpf_interval}s) -> {self.session_dir}"
            )

    def _stop_ebpf(self):
        proc = self._ebpf_proc
        if not proc:
            return
        try:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                print(
                    "  [!] eBPF tracer not responding - sending SIGTERM",
                    file=sys.stderr,
                )
                proc.terminate()
                proc.wait(timeout=5)
            if self.verbose:
                print(f"  [eBPF] tracer stopped (rc={proc.returncode})")
        except Exception as e:
            print(f"  [!] eBPF tracer stop error: {e}", file=sys.stderr)
        finally:
            self._ebpf_proc = None

    # ------------------------------------------------------------------ reports
    def _run_reports(self):
        if not self.reports or self.reports == "none":
            return
        try:
            from report.__main__ import main as report_main
        except ImportError as e:
            print(f"  [!] report module import failed: {e}", file=sys.stderr)
            return
        rc = report_main(
            [
                "--session-dir",
                self.session_dir,
                "--session-id",
                self.session_id,
                "--format",
                self.reports,
            ]
        )
        if rc:
            print(
                f"  [!] some reports failed (rc={rc}) - {self.session_dir}",
                file=sys.stderr,
            )
        elif self.verbose:
            print(f"  [*] reports written -> {self.session_dir}")
