# Baseline Resolution + Confidence Rating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the user's installed version isn't an exact changelog entry, pick a sensible baseline by educated guess, and show a High/Medium/Low confidence badge in the Discord digest.

**Architecture:** A single `resolve_baseline()` in `magnitude.py` decides which version blocks count as "new" and how confident that choice is. `pipeline.process` calls it once; the magnitude score, the delta field, and the confidence badge all derive from that one result, so they can never disagree. The embed gains an inline confidence badge.

**Tech Stack:** Python 3.12+, pydantic config, pytest. Run tests with `.venv/bin/python -m pytest`.

---

## File Structure

- `src/vnmaster/magnitude.py` — add `BaselineResolution` dataclass + `resolve_baseline()` and two private helpers. Existing `sum_since`/`version_strictly_after`/`parse_version`/`_best_version_token` stay.
- `src/vnmaster/digest/embeds.py` — add `confidence_badge()` and a `confidence` param to `build_update_embed`.
- `src/vnmaster/pipeline.py` — call `resolve_baseline`; derive score + deltas from `resolution.counted`; pass `confidence` to the embed. Simplify `_aggregate_deltas` to a pure summer (filtering moved to the resolver).
- `tests/unit/test_magnitude.py` — tests for `resolve_baseline` (one per resolution row).
- `tests/unit/test_digest_embeds.py` — badge assertions + new param on call sites.
- `tests/unit/test_pipeline_delta.py` — rewrite the `_aggregate_deltas` tests for the new pure-summer signature.

---

## Task 1: `resolve_baseline` + `BaselineResolution`

**Files:**
- Modify: `src/vnmaster/magnitude.py` (imports near line 10; add code after `sum_since`, ~line 63)
- Test: `tests/unit/test_magnitude.py`

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/unit/test_magnitude.py`. Import `resolve_baseline` by extending the existing import block from `vnmaster.magnitude` (add `resolve_baseline`). The existing `_v(version, **kw)` helper in this file defaults every metric to `None`, which Task 1 relies on for the all-null case.

```python
def _counted_versions(res) -> list[str]:
    return [v["version"] for v in res.counted]


def test_resolve_never_played_counts_all_high() -> None:
    res = resolve_baseline(
        [_v("0.2.0", renders=100), _v("0.1.0", renders=50)], None
    )
    assert res.confidence == "high"
    assert set(_counted_versions(res)) == {"0.2.0", "0.1.0"}


def test_resolve_exact_match_high() -> None:
    versions = [_v("0.7.0", renders=100), _v("0.6.0", renders=10),
                _v("0.5.0", renders=5)]
    res = resolve_baseline(versions, "0.6.0")
    assert res.confidence == "high"
    assert res.anchor == "0.6.0"
    assert _counted_versions(res) == ["0.7.0"]


def test_resolve_floor_match_medium() -> None:
    # Have 0.1.5; changelog lists 0.1.4 / 0.1.6 / 0.2.0 (no 0.1.5).
    versions = [_v("0.2.0", renders=20), _v("0.1.6", renders=10),
                _v("0.1.4", renders=5)]
    res = resolve_baseline(versions, "0.1.5")
    assert res.confidence == "medium"
    assert res.anchor == "0.1.4"
    assert set(_counted_versions(res)) == {"0.1.6", "0.2.0"}


def test_resolve_behind_whole_list_medium() -> None:
    versions = [_v("0.2.0", renders=20), _v("0.1.0", renders=10)]
    res = resolve_baseline(versions, "0.0.9")
    assert res.confidence == "medium"
    assert res.anchor is None
    assert set(_counted_versions(res)) == {"0.2.0", "0.1.0"}


def test_resolve_single_block_low() -> None:
    res = resolve_baseline([_v("0.2.0", renders=20)], "0.1.0")
    assert res.confidence == "low"
    assert _counted_versions(res) == ["0.2.0"]


def test_resolve_unparseable_installed_uses_latest_low() -> None:
    versions = [_v("0.2.0", renders=20), _v("0.1.0", renders=10)]
    res = resolve_baseline(versions, "Chapter_12_Patreon")
    assert res.confidence == "low"
    assert _counted_versions(res) == ["0.2.0"]  # latest only


def test_resolve_all_null_numbers_downgrades_to_low() -> None:
    # Exact version match, but the newer block has no usable metrics.
    versions = [_v("0.7.0"), _v("0.6.0", renders=10)]  # 0.7.0 metrics all None
    res = resolve_baseline(versions, "0.6.0")
    assert res.confidence == "low"
    assert _counted_versions(res) == ["0.7.0"]


def test_resolve_messy_label_floor_medium() -> None:
    versions = [_v("v.0.2.6f", renders=50), _v("v0.2.0p", renders=5)]
    res = resolve_baseline(versions, "v0.2.1p")
    assert res.confidence == "medium"
    assert res.anchor == "v0.2.0p"
    assert _counted_versions(res) == ["v.0.2.6f"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_magnitude.py -k resolve -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_baseline'`.

- [ ] **Step 3: Add the dataclass import**

In `src/vnmaster/magnitude.py`, the import block currently reads:

```python
from __future__ import annotations

import re
from typing import Any

from vnmaster.config import MagnitudeScoreConfig
```

Change it to add the dataclass import:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vnmaster.config import MagnitudeScoreConfig
```

- [ ] **Step 4: Add the resolver**

In `src/vnmaster/magnitude.py`, immediately after the `sum_since` function (which ends around line 63, before `def star_band`), insert:

```python
@dataclass(frozen=True)
class BaselineResolution:
    """Which changelog version blocks count as 'new to the user', plus how
    confident we are in the baseline choice.

    `counted` feeds both the magnitude score and the delta field. `anchor` is
    the version label we treated as the user's baseline (None when we counted
    everything or had to guess). `confidence` is "high" | "medium" | "low".
    """

    counted: list[dict[str, Any]]
    anchor: str | None
    confidence: str


_METRIC_KEYS = (
    "renders", "animations", "words", "scenes", "new_locations", "new_characters",
)


def _has_usable_numbers(blocks: list[dict[str, Any]]) -> bool:
    return any((b.get(k) or 0) for b in blocks for k in _METRIC_KEYS)


def _maybe_downgrade(res: BaselineResolution) -> BaselineResolution:
    # A clean version match with no usable numbers is still an untrustworthy
    # estimate — drop to low confidence. `counted` is left unchanged.
    if res.confidence != "low" and not _has_usable_numbers(res.counted):
        return BaselineResolution(res.counted, res.anchor, "low")
    return res


def resolve_baseline(
    versions: list[dict[str, Any]],
    installed_version: str | None,
) -> BaselineResolution:
    """Decide which version blocks are 'new' relative to the installed version.

    See docs/superpowers/specs/2026-06-14-baseline-resolution-confidence-design.md
    for the full table. In short: exact match -> high; nearest entry at or below
    (or behind the whole list) -> medium; single entry / unplaceable version ->
    low; any match with no usable numbers -> low.
    """
    labeled = [v for v in versions if v.get("version")]

    # Never played: every listed version is new content.
    if installed_version is None:
        return _maybe_downgrade(BaselineResolution(labeled, None, "high"))

    # (block, parsed_version) for blocks we can order.
    orderable: list[tuple[dict[str, Any], tuple[int, ...]]] = []
    for v in labeled:
        tok = _best_version_token(v["version"])
        if tok is not None:
            orderable.append((v, parse_version(tok)))

    inst_tok = _best_version_token(installed_version)

    # Can't place the user (unparseable installed version, or nothing
    # orderable): assume one version behind -> latest block only.
    if inst_tok is None or not orderable:
        if orderable:
            latest = max(orderable, key=lambda p: p[1])[0]
            return BaselineResolution([latest], None, "low")
        return BaselineResolution(labeled, None, "low")

    # Nothing before the latest to diff against.
    if len(orderable) == 1:
        return BaselineResolution([orderable[0][0]], None, "low")

    inst = parse_version(inst_tok)

    # Exact version match -> high.
    if any(pv == inst for _, pv in orderable):
        counted = [v for v, pv in orderable if pv > inst]
        return _maybe_downgrade(
            BaselineResolution(counted, installed_version, "high")
        )

    # Floor: highest listed version at or below the installed one -> medium.
    at_or_below = [(v, pv) for v, pv in orderable if pv <= inst]
    if at_or_below:
        floor_v, floor_pv = max(at_or_below, key=lambda p: p[1])
        counted = [v for v, pv in orderable if pv > floor_pv]
        return _maybe_downgrade(
            BaselineResolution(counted, floor_v["version"], "medium")
        )

    # Installed is older than every listed version -> behind the whole list.
    return _maybe_downgrade(
        BaselineResolution([v for v, _ in orderable], None, "medium")
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_magnitude.py -k resolve -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Run the full magnitude file to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/unit/test_magnitude.py -q`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add src/vnmaster/magnitude.py tests/unit/test_magnitude.py
git commit -m "feat: resolve_baseline picks a baseline version + confidence

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Confidence badge in the embed

**Files:**
- Modify: `src/vnmaster/digest/embeds.py`
- Test: `tests/unit/test_digest_embeds.py`

- [ ] **Step 1: Write/adjust the failing tests**

In `tests/unit/test_digest_embeds.py`, the first test (`test_update_embed_includes_version_delta_and_magnitude_stars`) currently calls `build_update_embed(...)` with `magnitude_score=5.0` and asserts `"★★★★★"` and `"~5h"`. Add `confidence="high"` to that call and a badge assertion. The call becomes:

```python
    embed = build_update_embed(
        sel,
        deltas={"renders": 800, "animations": 35, "words": 12000, "scenes": 3,
                "new_locations": 1, "new_characters": 1},
        magnitude_score=5.0,  # ~5 hours of added content → 5 stars
        summary_one_line="Maya story + university chapter",
        confidence="high",
    )
```

And add, just after the existing `assert "~5h" in field["value"]` line:

```python
    assert "🟢 High" in field["value"]
```

Then add a new test at the end of the file:

```python
def test_update_embed_renders_confidence_badges() -> None:
    sel = SelectedUpdate(
        f95_thread_id=1, game_title="X", installed_version="0.1",
        latest_upstream_version="0.2", upstream_last_updated_at=None,
        raw_changelog="", developer=None, image_url=None,
        upstream_thread_url=None, last_played_at=None, install_path=None,
        tags_json=None,
    )
    for level, badge in [("high", "🟢 High"), ("medium", "🟡 Medium"),
                         ("low", "🔴 Low")]:
        embed = build_update_embed(
            sel, deltas={}, magnitude_score=2.0,
            summary_one_line="", confidence=level,
        )
        field = next(f for f in embed["fields"]
                     if f["name"] == "Est. added playtime")
        assert badge in field["value"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_digest_embeds.py -v`
Expected: FAIL — `TypeError: build_update_embed() got an unexpected keyword argument 'confidence'`.

- [ ] **Step 3: Add the badge helper and the `confidence` param**

In `src/vnmaster/digest/embeds.py`, no import change is needed (`confidence_badge` is defined locally in this file). Add, just below `UPDATE_COLOR = 0xF0B232`:

```python
_CONFIDENCE_BADGES = {"high": "🟢 High", "medium": "🟡 Medium", "low": "🔴 Low"}


def confidence_badge(level: str) -> str:
    """Inline badge for the digest, e.g. '🟡 Medium'. Unknown -> low."""
    return _CONFIDENCE_BADGES.get(level, "🔴 Low")
```

Add the parameter to `build_update_embed`'s signature (keyword-only, default `"low"` so an un-wired caller fails safe toward least trust):

```python
def build_update_embed(
    sel: SelectedUpdate,
    *,
    deltas: dict[str, int],
    magnitude_score: float,
    summary_one_line: str,
    confidence: str = "low",
) -> dict[str, Any]:
```

Change the stars line from:

```python
    stars = f"{star_band(magnitude_score)} · {runtime_label(magnitude_score)}"
    summary_text = f"{stars}\n{summary_one_line}" if summary_one_line else stars
```

to:

```python
    stars = (
        f"{star_band(magnitude_score)} · {runtime_label(magnitude_score)} "
        f"· {confidence_badge(confidence)}"
    )
    summary_text = f"{stars}\n{summary_one_line}" if summary_one_line else stars
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_digest_embeds.py -v`
Expected: PASS (all, including the two unrelated footer/never-played tests which now render `🔴 Low` but don't assert on it).

- [ ] **Step 5: Commit**

```bash
git add src/vnmaster/digest/embeds.py tests/unit/test_digest_embeds.py
git commit -m "feat: show confidence badge in digest embed

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Wire the resolver through the pipeline

**Files:**
- Modify: `src/vnmaster/pipeline.py` (import line 24; loop body ~87-99; `_aggregate_deltas` ~276-289)
- Test: `tests/unit/test_pipeline_delta.py`

- [ ] **Step 1: Rewrite the `_aggregate_deltas` tests for the pure-summer signature**

In `tests/unit/test_pipeline_delta.py`, the four `_aggregate_deltas` tests pass a `user_version` and rely on the function filtering. Filtering now lives in `resolve_baseline` (covered by Task 1). Replace the whole `_aggregate_deltas` test section (the block between the `# _aggregate_deltas` banner and the `# _pick_summary_line` banner, i.e. the four `test_aggregate_deltas_*` functions) with these pure-summation tests:

```python
def test_aggregate_deltas_sums_metrics() -> None:
    versions = [_v("0.10.0", renders=50), _v("0.9.0", renders=30)]
    assert _aggregate_deltas(versions) == {"renders": 80}


def test_aggregate_deltas_skips_null_and_zero() -> None:
    # words/animations stay None; only renders is present.
    versions = [_v("0.10.0", renders=5)]
    assert _aggregate_deltas(versions) == {"renders": 5}


def test_aggregate_deltas_empty_returns_empty() -> None:
    assert _aggregate_deltas([]) == {}
```

Leave the three `_pick_summary_line` tests unchanged — that function keeps its `(versions, user_version)` signature.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_pipeline_delta.py -v`
Expected: FAIL — the new tests call `_aggregate_deltas(versions)` (one arg) but the function still requires two.

- [ ] **Step 3: Simplify `_aggregate_deltas`**

In `src/vnmaster/pipeline.py`, replace the whole function (currently lines ~276-289):

```python
def _aggregate_deltas(versions: list[dict[str, Any]], user_version: str | None) -> dict[str, int]:
    from vnmaster.magnitude import version_strictly_after
    versions = [v for v in versions if v.get("version")]
    if user_version is None:
        relevant = versions
    else:
        relevant = [v for v in versions if version_strictly_after(v["version"], user_version)]
    out: dict[str, int] = {}
    for key in ("renders", "animations", "words", "scenes",
                "new_locations", "new_characters"):
        total = sum((v.get(key) or 0) for v in relevant)
        if total:
            out[key] = total
    return out
```

with the pure summer (the caller now passes the already-filtered `counted` list):

```python
def _aggregate_deltas(versions: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("renders", "animations", "words", "scenes",
                "new_locations", "new_characters"):
        total = sum((v.get(key) or 0) for v in versions)
        if total:
            out[key] = total
    return out
```

- [ ] **Step 4: Run the `_aggregate_deltas` tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_pipeline_delta.py -v`
Expected: PASS (3 summation tests + 3 unchanged summary-line tests).

- [ ] **Step 5: Swap the pipeline import**

In `src/vnmaster/pipeline.py` line 24, change:

```python
from vnmaster.magnitude import sum_since
```

to:

```python
from vnmaster.magnitude import resolve_baseline, score_versions
```

- [ ] **Step 6: Use the resolver in the loop**

In `src/vnmaster/pipeline.py`, the loop body currently reads (lines ~92-98):

```python
        score = sum_since(result.versions, u.installed_version, cfg.magnitude_score)
        deltas = _aggregate_deltas(result.versions, u.installed_version)
        summary = _pick_summary_line(result.versions, u.installed_version)
        embed = build_update_embed(
            u, deltas=deltas, magnitude_score=score, summary_one_line=summary
        )
```

Replace it with:

```python
        resolution = resolve_baseline(result.versions, u.installed_version)
        score = score_versions(resolution.counted, cfg.magnitude_score)
        deltas = _aggregate_deltas(resolution.counted)
        summary = _pick_summary_line(result.versions, u.installed_version)
        embed = build_update_embed(
            u, deltas=deltas, magnitude_score=score,
            summary_one_line=summary, confidence=resolution.confidence,
        )
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: PASS (all). If `tests/integration` exists and runs offline, also run `.venv/bin/python -m pytest -q`.

- [ ] **Step 8: Commit**

```bash
git add src/vnmaster/pipeline.py tests/unit/test_pipeline_delta.py
git commit -m "feat: digest derives score, deltas, confidence from resolve_baseline

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes (author)

- **Spec coverage:** Rows 1-6 of the spec table → Task 1 steps + the 8 `resolve_baseline` tests (never-played, exact, floor, behind-all, single-block, unparseable, all-null override, messy-label). Embed badge (inline, no reason text) → Task 2. Single-resolver consistency (score + deltas from one decision) → Task 3.
- **`sum_since` retained:** Spec says keep it; it stays in `magnitude.py` with its existing tests even though `pipeline` no longer calls it. `resolve_baseline` reimplements filtering (rather than calling `sum_since`) because it must return the *counted blocks* and a confidence level, not just a float.
- **Failsafe default:** `confidence="low"` default on `build_update_embed` keeps the two confidence-agnostic embed tests green and means a future un-wired caller errs toward least trust. The pipeline always passes the real value (Task 3, Step 6).
- **Behavior preserved:** the old `_aggregate_deltas` filtering cases map onto `resolve_baseline` (e.g. user 0.5.0 with blocks 0.1.0/0.2.0 → floor 0.2.0, counted empty → empty deltas, same as before).
