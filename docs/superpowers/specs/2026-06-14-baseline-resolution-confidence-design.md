# Baseline resolution + confidence rating

**Date:** 2026-06-14
**Status:** Approved, pending implementation

## Problem

The digest estimates "added playtime since you last played" by filtering the
extracted changelog to version blocks strictly newer than the user's installed
version (`magnitude.sum_since` / `pipeline._aggregate_deltas`). When the
installed version is **not present** in the changelog blob, `version_strictly_after`
returns `False` conservatively, which silently **excludes** content and
undercounts — or, if the version is unparseable, drops everything.

The user wants an educated guess with sane defaults instead of silent
undercounting, plus a visible confidence signal so they know how much to trust
the estimate. Precision is not required; this is a rough "worth re-downloading?"
indicator.

## Goals

- When the installed version is missing from the changelog, pick a sensible
  baseline anchor instead of falling back to nothing.
- Surface a confidence level (High / Medium / Low) in the Discord embed.
- Keep the magnitude score and the delta field consistent — both must reflect
  the same baseline decision.

## Non-goals

- No change to the LLM extraction prompt or schema. Confidence is derived from
  version matching and presence of numbers, **not** from asking Claude to
  self-rate its extraction.
- No new data sources for the installed version (still disk scan → save file →
  none, per `matcher.py`).

## Design

### Single resolver

Add `resolve_baseline(versions, installed_version) -> BaselineResolution` to
`magnitude.py`. It is the one place that decides which version blocks count.

```python
@dataclass(frozen=True)
class BaselineResolution:
    counted: list[dict[str, Any]]   # version blocks to sum for score + deltas
    anchor: str | None              # the version label used as the baseline
    confidence: str                 # "high" | "medium" | "low"
```

`pipeline.process` calls it once per update, then derives everything from
`resolution.counted`:

- `score = score_versions(resolution.counted, weights)`
- `deltas = aggregate_deltas(resolution.counted)`  (no longer re-filters)
- `confidence = resolution.confidence` → passed to the embed builder

`sum_since` and `version_strictly_after` remain (still used by `resolve_baseline`
and existing tests). `_aggregate_deltas` is changed to take the already-filtered
`counted` list rather than re-deriving the filter from `installed_version`.

### Resolution algorithm

Let **blocks** = version entries with a parseable dotted version token
(`_best_version_token`). **latest** = the block with the max parsed version.

| # | Situation | `counted` | `confidence` |
|---|-----------|-----------|--------------|
| 1 | `installed_version is None` (never played) | all blocks | high |
| 2 | A block's version **equals** installed (parsed) | blocks strictly newer than installed | high |
| 3 | No exact match, but ≥1 block **≤ installed** (floor) | blocks strictly newer than the floor | medium |
| 4 | installed **< every** block (behind the whole list) | all blocks | medium |
| 5a | exactly **one** parseable block (nothing before latest) | that block | low |
| 5b | installed unparseable, or no parseable blocks to order | latest block only (assume one behind); if no parseable blocks, all blocks | low |
| 6 | *(override)* every metric across `counted` is null/0 | unchanged | low |

Notes:
- "Floor" = the highest block whose parsed version is `<= installed`. Rows 3 and
  4 together cover every in-range / below-range case for a parseable installed
  version: if a floor exists use it (medium), otherwise the user is behind the
  whole list so count all (medium).
- Row 6 is a downgrade applied **after** `counted` is chosen: a clean version
  match with no usable numbers still yields an untrustworthy estimate, so it
  reads Low. It never changes `counted`, only `confidence`.
- Version comparison reuses `parse_version` / `_best_version_token` so messy
  labels (`v.0.2.6f`, `Week 3 v3.6.14`) normalize the same way they do today.

### Embed change

`build_update_embed` gains a `confidence: str` parameter. The playtime line
appends an inline badge:

```
★★★ · ~3h · 🟡 Medium
```

Badge map: `high → 🟢 High`, `medium → 🟡 Medium`, `low → 🔴 Low`. No reason
text (inline badge only). A small `confidence_badge(level)` helper lives in
`embeds.py` (it is display formatting) and returns e.g. `"🟡 Medium"`.

## Data flow

```
raw_changelog (F95Checker blob)
  → LLM extract → versions: list[block]
  → resolve_baseline(versions, installed_version)
       → BaselineResolution(counted, anchor, confidence)
  → score   = score_versions(counted, weights)
  → deltas  = aggregate_deltas(counted)
  → embed   = build_update_embed(..., magnitude_score=score,
                                 deltas=deltas, confidence=confidence)
```

## Testing

`tests/unit/test_magnitude.py` (or a new `test_baseline.py`):

- Row 1: `installed=None` → all counted, high.
- Row 2: exact match → newer-than-installed counted, high.
- Row 3: floor match (have 0.1.5, blocks 0.1.4/0.1.6/0.2.0) → counts 0.1.6+0.2.0, medium.
- Row 4: behind all (have 0.0.9, blocks 0.1.x+) → all counted, medium.
- Row 5a: single block → that block, low.
- Row 5b: unparseable installed → latest block only, low.
- Row 6: matched version but all-null metrics → low override.
- Messy-label regression (reuse existing `v.0.2.6f` vs `v0.2.1p`).

`tests/unit/test_digest_embeds.py`:

- Badge renders for each confidence level in the playtime field.

## Risks / edge cases

- Duplicate or unparseable version labels: unparseable blocks are ignored for
  ordering but still counted when the whole list is unorderable (5b tail).
- "Behind the whole list" (row 4) can over-count if the changelog blob is
  truncated and the user is actually only one version behind. Acceptable: the
  Medium badge signals the estimate is a guess, and over-counting is the safer
  failure for a "worth re-downloading?" prompt than silent under-counting.
