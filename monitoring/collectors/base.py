"""Collector — the abstract contract every observation source implements.

A collector owns one data stream: it knows how to start, how to stop, and
where its artifacts land (always inside the session directory it is given).
The Session orchestrates a set of collectors for the lifetime of a workload.

Adding a new collector (network stats, perf counters, another eBPF tracer)
means: subclass Collector, implement start()/stop(), register a name. The
Session and CLI need no changes beyond listing the new name.
"""

import sys
from abc import ABC, abstractmethod


class Collector(ABC):
    """One observation source with a start/stop lifecycle.

    Subclasses set a class-level ``name`` and implement ``start``/``stop``.
    ``start`` must not raise — collectors are best-effort; a failed collector
    should print a warning and leave the rest of the session intact.
    """

    name = "collector"

    @abstractmethod
    def start(self, session_dir, session_id, sys_info):
        """Begin collecting into session_dir. Must not raise."""

    @abstractmethod
    def stop(self):
        """Stop collecting and flush artifacts. Must not raise."""

    def _warn(self, msg):
        print(f"  [!] [{self.name}] {msg}", file=sys.stderr)
