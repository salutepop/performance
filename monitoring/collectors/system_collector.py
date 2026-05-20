"""SystemCollector — wraps SystemMonitor as a Collector.

SystemMonitor itself is the /proc + /sys + nvidia-smi poller (system.py).
This thin adapter gives it the uniform Collector lifecycle so the Session
can treat it like any other collector.
"""

from .base import Collector
from .system import SystemMonitor


class SystemCollector(Collector):
    name = "system"

    def __init__(self, interval=1.0):
        self.interval = float(interval)
        self._mon = None

    def start(self, session_dir, session_id, sys_info):
        try:
            self._mon = SystemMonitor(
                session_dir,
                session_id=session_id,
                interval=self.interval,
                sys_info=sys_info,
            )
            self._mon.start()
        except Exception as e:
            self._mon = None
            self._warn(f"start failed (reports will use fio JSON only): {e}")

    def stop(self):
        if not self._mon:
            return
        try:
            self._mon.stop()
        except Exception as e:
            self._warn(f"stop error: {e}")
        finally:
            self._mon = None
