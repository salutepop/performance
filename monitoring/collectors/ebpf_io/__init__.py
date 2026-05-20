"""eBPF block-layer I/O tracer collector.

Python orchestrator (collector.py) drives the native io_trace binary built
from src/. See CLAUDE.md for the full architecture (Q2D/D2C/U2Q/C2A/A2U
phases, BPF maps, JSON contract between layers).

Build:
    make -C monitoring/collectors/ebpf_io/src
"""
