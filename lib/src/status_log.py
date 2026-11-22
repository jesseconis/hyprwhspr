"""Log a background component's availability only when it changes.

Retry loops for optional integrations (a remote ducking hook, the voxd input
lease) can fail every second for as long as the peer is away. Printing each
attempt buries useful output, so callers report every outcome here and only
transitions reach the terminal: the first failure, the recovery, and an
optional one-time "ready" line.
"""

from __future__ import annotations

import threading
from typing import Optional


class StatusReporter:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self._lock = threading.Lock()
        self._up: Optional[bool] = None  # None until the first outcome

    @property
    def is_down(self) -> bool:
        with self._lock:
            return self._up is False

    def _emit(self, message: str):
        print(f"{self.prefix} {message}", flush=True)

    def up(self, first: Optional[str] = None, recovered: Optional[str] = "available again"):
        """Record success; print ``first`` once, or ``recovered`` after an outage."""
        with self._lock:
            previous, self._up = self._up, True
        if previous is None and first:
            self._emit(first)
        elif previous is False and recovered:
            self._emit(recovered)

    def down(self, message: str) -> bool:
        """Record a failure; print ``message`` only when newly unavailable.

        Returns True when the message was printed.
        """
        with self._lock:
            previous, self._up = self._up, False
        if previous is False:
            return False
        self._emit(message)
        return True
