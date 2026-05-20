"""Compat shim — Session moved to monitoring/session.py."""

from monitoring.session import Session, ebpf_available, resolve_ebpf_mode  # noqa: F401
