"""Magnitude score computation.

The "score" is an *estimated added playtime in hours*, derived from the
length-bearing content metrics (renders, words, animations) via the weights
in `MagnitudeScoreConfig`. `score_versions` sums a list of version dicts;
`sum_since` first filters to versions strictly newer than `user_version`.
`star_band` maps the hour estimate to a 1-5 star label (stars ≈ hours) for
the Discord embed, and `runtime_label` formats it as a short "~Nh" string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vnmaster.config import MagnitudeScoreConfig


# Matches dotted-numeric version tokens such as "0.4", "0.20.3.1", "7.0".
# The `+` on the dot-group guarantees at least one dot, so bare integers
# like "4" or "7" are naturally excluded.
_VERSION_TOKEN_RE = re.compile(r"\d+(?:\.\d+)+")


def version_tokens(s: str | None) -> set[str]:
    """Dotted numeric version tokens from an arbitrary version string.

    '0.4RC1' -> {'0.4'}; 'Ch.4 v0.4' -> {'0.4'}; 'v0.20.3.1' -> {'0.20.3.1'}.
    Only tokens containing a dot are returned (single integers excluded).
    """
    if not s:
        return set()
    return set(_VERSION_TOKEN_RE.findall(s))


def score_versions(
    versions: list[dict[str, Any]], weights: MagnitudeScoreConfig
) -> float:
    total = 0.0
    for v in versions:
        if v.get("bugfix_only"):
            total += weights.bugfix_only_penalty
            continue
        total += (v.get("renders") or 0) * weights.renders
        total += (v.get("animations") or 0) * weights.animations
        total += ((v.get("words") or 0) / 1000.0) * weights.words_per_1k
        total += (v.get("scenes") or 0) * weights.scenes
        total += (v.get("new_locations") or 0) * weights.new_locations
        total += (v.get("new_characters") or 0) * weights.new_characters
    return total


def sum_since(
    versions: list[dict[str, Any]],
    user_version: str | None,
    weights: MagnitudeScoreConfig,
) -> float:
    # Skip entries missing a version label — protects against malformed
    # LLM tool output that omits the required field.
    versions = [v for v in versions if v.get("version")]
    if user_version is None:
        return score_versions(versions, weights)
    newer = [v for v in versions if version_strictly_after(v["version"], user_version)]
    return score_versions(newer, weights)


@dataclass(frozen=True)
class BaselineResolution:
    """Which changelog version blocks count as 'new to the user', plus how
    confident we are in the baseline choice.

    `counted` feeds both the magnitude score and the delta field. `anchor` is
    the version label we treated as the user's baseline (None when we counted
    everything or had to guess). `confidence` is "high" | "medium" | "low".
    `basis` is a short (2-3 word) label of how the baseline was chosen, shown
    in the digest's "Accuracy" column (e.g. "nearest version", "latest only").
    """

    counted: list[dict[str, Any]]
    anchor: str | None
    confidence: str
    basis: str


_METRIC_KEYS = (
    "renders", "animations", "words", "scenes", "new_locations", "new_characters",
)


def _has_usable_numbers(blocks: list[dict[str, Any]]) -> bool:
    return any((b.get(k) or 0) for b in blocks for k in _METRIC_KEYS)


def _maybe_downgrade(res: BaselineResolution) -> BaselineResolution:
    # A clean version match with no usable numbers is still an untrustworthy
    # estimate — drop to low confidence. `counted` is left unchanged.
    if res.confidence != "low" and not _has_usable_numbers(res.counted):
        return BaselineResolution(res.counted, res.anchor, "low", "no figures listed")
    return res


def _block_token(block: dict[str, Any]) -> str | None:
    """Comparable dotted version token for a changelog block.

    Prefer the LLM-supplied `version_normalized` — the model interprets messy
    or range labels (e.g. "v0.9.2-5" -> "0.9.5") far better than a regex can.
    Fall back to parsing the raw `version` label when it's absent/unusable
    (older cached extractions, regex-fallback rows, model returned null).
    """
    norm = block.get("version_normalized")
    if norm:
        tok = _best_version_token(norm)
        if tok is not None:
            return tok
    return _best_version_token(block.get("version"))


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
        return _maybe_downgrade(BaselineResolution(labeled, None, "high", "all new"))

    # (block, parsed_version) for blocks we can order.
    orderable: list[tuple[dict[str, Any], tuple[int, ...]]] = []
    for v in labeled:
        tok = _block_token(v)
        if tok is not None:
            orderable.append((v, parse_version(tok)))

    inst_tok = _best_version_token(installed_version)

    # Can't place the user (unparseable installed version, or nothing
    # orderable): assume one version behind -> latest block only.
    if inst_tok is None or not orderable:
        if orderable:
            latest = max(orderable, key=lambda p: p[1])[0]
            return BaselineResolution([latest], None, "low", "version unreadable")
        return BaselineResolution(labeled, None, "low", "versions unreadable")

    # Nothing before the latest to diff against.
    if len(orderable) == 1:
        return BaselineResolution([orderable[0][0]], None, "low", "latest only")

    inst = parse_version(inst_tok)

    # Exact version match -> high.
    if any(pv == inst for _, pv in orderable):
        counted = [v for v, pv in orderable if pv > inst]
        return _maybe_downgrade(
            BaselineResolution(counted, installed_version, "high", "version match")
        )

    # Floor: highest listed version at or below the installed one -> medium.
    at_or_below = [(v, pv) for v, pv in orderable if pv <= inst]
    if at_or_below:
        floor_v, floor_pv = max(at_or_below, key=lambda p: p[1])
        counted = [v for v, pv in orderable if pv > floor_pv]
        return _maybe_downgrade(
            BaselineResolution(counted, floor_v["version"], "medium", "nearest version")
        )

    # Installed is older than every listed version -> behind the whole list.
    return _maybe_downgrade(
        BaselineResolution([v for v, _ in orderable], None, "medium", "behind all")
    )


def changelog_behind_upstream(
    versions: list[dict[str, Any]], upstream_version: str | None
) -> bool:
    """True when the newest changelog entry is OLDER than the reported upstream
    version — i.e. F95Checker shows a newer release than the changelog text
    describes (the dev bumped the version but hasn't posted its notes yet).

    In that state any "what's new" estimate is missing the latest version, so
    the digest should say so rather than imply nothing changed. Conservative:
    returns False when either side has no parseable version.
    """
    if not upstream_version:
        return False
    up = _best_version_token(upstream_version)
    if up is None:
        return False
    newest: tuple[int, ...] | None = None
    for v in versions:
        tok = _block_token(v)
        if tok is None:
            continue
        pv = parse_version(tok)
        if newest is None or pv > newest:
            newest = pv
    if newest is None:
        return False
    return parse_version(up) > newest


def star_band(score: float) -> str:
    # `score` is estimated added playtime in hours; stars ≈ rounded hours.
    # ~1h of new content is 1★, ~5h+ is 5★. A 10-minute update reads as 1★.
    if score < 1.5:
        return "★"
    if score < 2.5:
        return "★★"
    if score < 3.5:
        return "★★★"
    if score < 4.5:
        return "★★★★"
    return "★★★★★"


def runtime_label(score: float) -> str:
    """Short human label for the estimated added playtime, e.g. '~3h'.

    Rounds to the nearest half hour; anything under ~45 min shows '<1h'.
    """
    if score < 0.75:
        return "<1h"
    rounded = round(score * 2) / 2
    text = f"{rounded:g}"  # 3.0 -> "3", 3.5 -> "3.5"
    return f"~{text}h"


def parse_version(label: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in label.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if digits:
            parts.append(int(digits))
        else:
            parts.append(0)
    return tuple(parts)


def _best_version_token(s: str | None) -> str | None:
    """Most-specific dotted-numeric version token in a free-form label.

    'Week 3 v3.6.14' -> '3.6.14', 'v0.7.1' -> '0.7.1', 'Ch. 12 Official' -> None.
    Picks the token with the most dot-segments, breaking ties by numeric value.
    """
    toks = version_tokens(s)
    if not toks:
        return None
    return max(toks, key=lambda t: (t.count("."), parse_version(t)))


def version_strictly_after(candidate: str | None, baseline: str | None) -> bool:
    """True if candidate's normalized version is strictly newer than baseline's.

    Uses the most-specific dotted token from each side, so messy labels like
    'v.0.2.6f' compare correctly against 'v0.2.1p'. Returns False when either
    side has no parseable dotted token (conservative).
    """
    ct, bt = _best_version_token(candidate), _best_version_token(baseline)
    if ct is None or bt is None:
        return False
    return parse_version(ct) > parse_version(bt)


def is_user_behind(installed: str | None, upstream: str | None) -> bool:
    """True only when the user's version is CONFIDENTLY older than upstream.

    Conservative: missing/unparseable/equal versions return False (genuinely
    new releases still surface via the timestamp path in select.py).
    """
    if not installed or not upstream:
        return False
    iv, uv = _best_version_token(installed), _best_version_token(upstream)
    if iv is None or uv is None:
        return False
    return parse_version(iv) < parse_version(uv)
