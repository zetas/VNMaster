"""Tracks LLM spend per process.

For v1.0 we keep it in memory; a persistent variant reading/writing to
vnmaster.db could be added in v1.1.
"""
from __future__ import annotations


class InMemoryBudget:
    def __init__(self, cap_usd: float) -> None:
        self._cap = cap_usd
        self._spent = 0.0

    def remaining_usd(self) -> float:
        return max(0.0, self._cap - self._spent)

    def record(self, cost_usd: float) -> None:
        self._spent += cost_usd

    def reset(self) -> None:
        self._spent = 0.0

    @property
    def spent_usd(self) -> float:
        return self._spent
