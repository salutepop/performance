"""Collectors — individual data streams (system stats, eBPF tracers, ...).

A collector owns the lifecycle of one observation source: it knows how to
start, stop, and where to write its artifacts. The Session orchestrates a
set of collectors for the duration of a workload.
"""

from .system import SystemMonitor

__all__ = ["SystemMonitor"]
