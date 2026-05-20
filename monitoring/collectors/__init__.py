"""Collectors — individual observation sources (system stats, eBPF tracers, ...).

A collector owns the lifecycle of one data stream: it knows how to start,
stop, and where to write its artifacts. The Session orchestrates a set of
collectors for the duration of a workload.

Implement a new one by subclassing Collector (base.py).
"""

from .base import Collector
from .system import SystemMonitor
from .system_collector import SystemCollector
from .ebpf_io import EbpfIoCollector

__all__ = ["Collector", "SystemMonitor", "SystemCollector", "EbpfIoCollector"]
