"""Tests for _aggregate_deltas and _pick_summary_line.

Both are pure consumers of the already-resolved `counted` block list
(version filtering lives in magnitude.resolve_baseline): _aggregate_deltas
sums the metric keys, _pick_summary_line returns the first non-empty summary.
"""
from __future__ import annotations

from vnmaster.magnitude import resolve_baseline
from vnmaster.pipeline import (
    _aggregate_deltas,
    _delta_note,
    _embed_signals,
    _pick_summary_line,
)


def _v(version: str, *, renders: int = 0, summary: str = "") -> dict:
    return {
        "version": version,
        "renders": renders,
        "animations": None,
        "words": None,
        "scenes": None,
        "new_locations": None,
        "new_characters": None,
        "bugfix_only": False,
        "summary_one_line": summary,
    }


# ---------------------------------------------------------------------------
# _aggregate_deltas
# ---------------------------------------------------------------------------

def test_aggregate_deltas_sums_metrics() -> None:
    versions = [_v("0.10.0", renders=50), _v("0.9.0", renders=30)]
    assert _aggregate_deltas(versions) == {"renders": 80}


def test_aggregate_deltas_skips_null_and_zero() -> None:
    # words/animations stay None; only renders is present.
    versions = [_v("0.10.0", renders=5)]
    assert _aggregate_deltas(versions) == {"renders": 5}


def test_aggregate_deltas_empty_returns_empty() -> None:
    assert _aggregate_deltas([]) == {}


# ---------------------------------------------------------------------------
# _delta_note (shown when there are no numeric deltas)
# ---------------------------------------------------------------------------

def test_delta_note_nothing_new_when_counted_empty() -> None:
    assert _delta_note([]) == "Nothing new in the changelog"


def test_delta_note_bugfixes_only_when_all_bugfix() -> None:
    counted = [
        {"version": "0.3.3", "bugfix_only": True},
        {"version": "0.3.2", "bugfix_only": True},
    ]
    assert _delta_note(counted) == "Bug fixes only"


def test_delta_note_unquantified_when_content_without_counts() -> None:
    # e.g. Eternum "a few renders & audio" / Inanna "translations" — real
    # changes the dev didn't put numbers on.
    counted = [{"version": "0.9.2-5", "bugfix_only": False}]
    assert _delta_note(counted) == "Changes listed, but no counts given"


# ---------------------------------------------------------------------------
# _embed_signals (stale-changelog overlay)
# ---------------------------------------------------------------------------

def test_embed_signals_normal_passes_resolution_through() -> None:
    res = resolve_baseline([_v("0.9.0", renders=10), _v("0.8.0", renders=5)], "0.8.0")
    confidence, basis, note = _embed_signals(res, stale=False)
    assert confidence == res.confidence
    assert basis == res.basis
    assert note == _delta_note(res.counted)


def test_embed_signals_stale_overrides_to_notes_not_posted() -> None:
    res = resolve_baseline([_v("0.9.0", renders=10)], "0.8.0")
    confidence, basis, note = _embed_signals(res, stale=True)
    assert confidence == "low"
    assert basis == "notes not posted"
    assert "no changelog notes" in note


# ---------------------------------------------------------------------------
# _pick_summary_line
# ---------------------------------------------------------------------------

def test_pick_summary_line_returns_first_nonempty() -> None:
    versions = [_v("0.10.0", summary=""), _v("0.9.0", summary="Intro chapter")]
    assert _pick_summary_line(versions) == "Intro chapter"


def test_pick_summary_line_empty_list_returns_empty_string() -> None:
    assert _pick_summary_line([]) == ""


def test_pick_summary_line_all_blank_summaries_returns_empty_string() -> None:
    versions = [_v("0.10.0", summary=""), _v("0.9.0", summary="")]
    assert _pick_summary_line(versions) == ""
