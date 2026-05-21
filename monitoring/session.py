"""
Session — runs a set of collectors for the lifetime of a workload, then reports.

A Session is the unit of observation: a directory on disk plus the collectors
writing into it. Workloads (fio, scripts, TC scenarios) run *inside* a Session
context; the Session doesn't care what runs, only that the observation window
is correctly opened and closed.

Usage:
    # explicit collector list
    with Session(session_dir, sys_info,
                 collectors=[SystemCollector(), EbpfIoCollector()],
                 reports="all"):
        run_workload(...)

    # or let Session build the default list from ebpf_mode (backward compat)
    with Session(session_dir, sys_info, ebpf_mode="on", reports="all"):
        run_workload(...)

On exit, collectors are stopped in reverse start order, then the configured
report formats are rendered in-place against the session directory.
"""

import os
import sys

from .collectors.ebpf_io import EbpfIoCollector, ebpf_available
from .collectors.system_collector import SystemCollector


def resolve_ebpf_mode(toggle):
    """Resolve --ebpf {auto,on,off} into "on" or "off".

    The tracer auto-detects engine (libaio/io_uring) and transport (pcie/rdma/
    tcp) at runtime, so there is no engine mode to pick — only whether eBPF
    tracing runs at all. Exits rc=1 if toggle="on" but the binary is missing.
    """
    if toggle == "off":
        return "off"
    if toggle == "on":
        if not ebpf_available():
            print(
                "[Error] --ebpf on but io_trace binary missing or not executable. "
                "Build with `make -C monitoring/collectors/ebpf_io/src` first.",
                file=sys.stderr,
            )
            sys.exit(1)
        return "on"
    # auto
    return "on" if ebpf_available() else "off"


def default_collectors(ebpf_mode="off", ebpf_interval=1.0, monitor_interval=1.0,
                       verbose=True):
    """Build the standard collector list: system always, eBPF when requested.

    ebpf_mode is "on"/"off" — the tracer auto-detects engine and transport.
    """
    collectors = [SystemCollector(interval=monitor_interval)]
    if ebpf_mode and ebpf_mode != "off":
        collectors.append(
            EbpfIoCollector(interval=ebpf_interval, verbose=verbose)
        )
    return collectors


class Session:
    """Context manager that runs collectors for the lifetime of a workload.

    Parameters
    ----------
    session_dir : str
        Absolute path to the session output directory. Created if missing.
    sys_info : dict
        Discovered system info (handed to collectors; included in metadata).
    collectors : list[Collector] or None
        Explicit collector list. If None, built from ebpf_mode via
        default_collectors().
    ebpf_mode : str
        Used only when collectors is None. "on" or "off" — the tracer
        auto-detects engine/transport, so there is no engine mode to pick.
    ebpf_interval / monitor_interval : float
        Polling intervals, used only when collectors is None.
    reports : str
        "none", "all", or a comma-list of md,json,png,pdf.
    verbose : bool
        Print lifecycle messages.
    """

    def __init__(
        self,
        session_dir,
        sys_info,
        collectors=None,
        ebpf_mode="off",
        ebpf_interval=1.0,
        monitor_interval=1.0,
        reports="none",
        verbose=True,
    ):
        self.session_dir = session_dir
        self.sys_info = sys_info
        self.reports = reports or "none"
        self.verbose = verbose

        if collectors is None:
            collectors = default_collectors(
                ebpf_mode=ebpf_mode,
                ebpf_interval=ebpf_interval,
                monitor_interval=monitor_interval,
                verbose=verbose,
            )
        self.collectors = collectors
        self._started = []

        os.makedirs(self.session_dir, exist_ok=True)

    @property
    def session_id(self):
        return os.path.basename(self.session_dir.rstrip(os.sep))

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self):
        for collector in self.collectors:
            collector.start(self.session_dir, self.session_id, self.sys_info)
            self._started.append(collector)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Stop in reverse start order (e.g. eBPF tracer before SystemMonitor).
        for collector in reversed(self._started):
            collector.stop()
        self._started = []
        self._run_reports()
        return False  # never suppress exceptions

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
                "--session-dir", self.session_dir,
                "--session-id", self.session_id,
                "--format", self.reports,
            ]
        )
        if rc:
            print(f"  [!] some reports failed (rc={rc}) - {self.session_dir}",
                  file=sys.stderr)
        elif self.verbose:
            print(f"  [*] reports written -> {self.session_dir}")


# Re-export so `from monitoring.session import ebpf_available` keeps working.
__all__ = ["Session", "default_collectors", "ebpf_available", "resolve_ebpf_mode"]
