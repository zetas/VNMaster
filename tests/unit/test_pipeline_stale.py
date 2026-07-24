"""Tests for the digest staleness warning helper."""
from __future__ import annotations

from datetime import datetime

from vnmaster.pipeline import STALE_REFRESH_SECONDS, _stale_warning

NOW = 1_784_100_000


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%b %d %H:%M")


def test_fresh_refresh_returns_none() -> None:
    last = NOW - 3600
    assert _stale_warning(last, NOW) is None


def test_exactly_at_threshold_returns_none() -> None:
    last = NOW - STALE_REFRESH_SECONDS
    assert _stale_warning(last, NOW) is None


def test_none_input_returns_none() -> None:
    assert _stale_warning(None, NOW) is None


def test_stale_below_48h_renders_hours() -> None:
    last = NOW - 31 * 3600
    warning = _stale_warning(last, NOW)
    assert warning == (
        "F95Checker data is stale — last successful refresh was "
        f"31 hours ago ({_stamp(last)}). Open F95Checker and hit Refresh."
    )


def test_stale_at_48h_and_above_renders_days() -> None:
    last = NOW - 3 * 86400
    warning = _stale_warning(last, NOW)
    assert warning == (
        "F95Checker data is stale — last successful refresh was "
        f"3 days ago ({_stamp(last)}). Open F95Checker and hit Refresh."
    )


def test_boundary_47h_hours_48h_days() -> None:
    warning_47 = _stale_warning(NOW - 47 * 3600, NOW)
    warning_48 = _stale_warning(NOW - 48 * 3600, NOW)
    assert "47 hours ago" in warning_47
    assert "2 days ago" in warning_48


def test_warning_has_no_ping() -> None:
    """Call sites decide whether to prefix @everyone; the helper never does."""
    warning = _stale_warning(NOW - 3 * 86400, NOW)
    assert "@everyone" not in warning
