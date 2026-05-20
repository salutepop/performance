"""Monitoring platform — discovery, system collectors, session lifecycle.

This is the primary surface of the project. A `Session` opens an observation
window; collectors run inside it and write per-session artifacts to disk.
Workloads (fio, scripts, TC scenarios) are optional inputs fed into a Session.
"""

from .session import Session, ebpf_available, resolve_ebpf_mode
from .discovery import SystemDiscovery

__all__ = ["Session", "SystemDiscovery", "ebpf_available", "resolve_ebpf_mode"]
