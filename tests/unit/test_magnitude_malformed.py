"""Regression tests: magnitude functions must tolerate malformed LLM output.

The Anthropic tool schema marks `version` as required, but in practice the
model can return partial entries (empty version, missing key). These must
not crash the digest pipeline.
"""
from __future__ import annotations

import pytest

from vnmaster.config import MagnitudeScoreConfig
from vnmaster.magnitude import sum_since

WEIGHTS = MagnitudeScoreConfig()


def test_sum_since_skips_entries_without_version_key() -> None:
    versions = [
        {"version": "0.7.0", "renders": 100, "bugfix_only": False},
        {"renders": 999, "bugfix_only": False},  # no `version` key
    ]
    # Must not raise KeyError. Only the well-formed entry counts.
    score = sum_since(versions, user_version=None, weights=WEIGHTS)
    assert score == pytest.approx(0.1)  # 100 renders ≈ 0.1h


def test_sum_since_skips_entries_with_empty_version() -> None:
    versions = [
        {"version": "0.7.0", "renders": 100, "bugfix_only": False},
        {"version": "", "renders": 999, "bugfix_only": False},
    ]
    score = sum_since(versions, user_version=None, weights=WEIGHTS)
    assert score == pytest.approx(0.1)  # 100 renders ≈ 0.1h


def test_sum_since_with_user_version_filters_correctly_after_skipping() -> None:
    versions = [
        {"version": "0.7.0", "renders": 100, "bugfix_only": False},
        {"renders": 200, "bugfix_only": False},  # skipped
        {"version": "0.5.0", "renders": 50, "bugfix_only": False},
    ]
    # Only 0.7.0 is newer than 0.6.0 (after the malformed one is skipped).
    score = sum_since(versions, user_version="0.6.0", weights=WEIGHTS)
    assert score == pytest.approx(0.1)  # 100 renders ≈ 0.1h
