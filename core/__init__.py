"""Backward-compat shim. Real modules live under monitoring/ now.

Kept until Phase 6 removes the last external user (currently:
ebpf/io_profiler.py uses `from core.monitor import SystemMonitor`).
"""
