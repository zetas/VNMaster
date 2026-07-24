"""Time abstraction so tests can pin 'now' deterministically."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def now_epoch(self) -> int: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    def now_epoch(self) -> int:
        return int(self.now().timestamp())
