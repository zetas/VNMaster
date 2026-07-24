import pytest

from vnmaster.config import MagnitudeScoreConfig
from vnmaster.magnitude import (
    _best_version_token,
    changelog_behind_upstream,
    is_user_behind,
    resolve_baseline,
    score_versions,
    star_band,
    sum_since,
    version_strictly_after,
)


WEIGHTS = MagnitudeScoreConfig()


def _v(version: str, **kw) -> dict:
    base = {
        "version": version, "renders": None, "animations": None, "words": None,
        "scenes": None, "new_locations": None, "new_characters": None,
        "bugfix_only": False,
    }
    base.update(kw)
    return base


def test_score_renders_only() -> None:
    s = score_versions([_v("0.7.0", renders=1000)], WEIGHTS)
    assert s == 1.0  # 1000 renders ≈ 1 hour (renders weight 0.001)


def test_score_animations_weighted_higher() -> None:
    s = score_versions([_v("0.7.0", animations=100)], WEIGHTS)
    assert s == 0.5  # animations weight 0.005 = 5× a render


def test_score_words_per_1k() -> None:
    s = score_versions([_v("0.7.0", words=10000)], WEIGHTS)
    assert s == 1.0  # ~10k words ≈ 1 hour (words_per_1k 0.1)


def test_ignored_metrics_add_nothing() -> None:
    s = score_versions(
        [_v("0.7.0", scenes=5, new_locations=3, new_characters=2)], WEIGHTS
    )
    assert s == 0.0  # scenes/locations/characters don't affect runtime


def test_bugfix_only_adds_no_playtime() -> None:
    s = score_versions([_v("0.7.0", bugfix_only=True)], WEIGHTS)
    assert s == 0.0


def test_summed_across_versions() -> None:
    s = score_versions(
        [_v("0.7.0", renders=1000), _v("0.6.0", renders=2000)], WEIGHTS
    )
    assert s == 3.0


@pytest.mark.parametrize(
    "score, band",
    [(0.5, "★"), (2, "★★"), (3, "★★★"), (4, "★★★★"), (5, "★★★★★")],
)
def test_star_band_thresholds(score: float, band: str) -> None:
    assert star_band(score) == band


def test_sum_since_uses_version_compare() -> None:
    versions = [
        _v("0.7.0", renders=100),
        _v("0.6.5", renders=50),
        _v("0.6.0", renders=200),
        _v("0.5.2", renders=1000),
    ]
    s = sum_since(versions, user_version="0.6.0", weights=WEIGHTS)
    assert s == pytest.approx(0.15)  # only 0.7.0 (100) + 0.6.5 (50) renders count


def test_sum_since_none_user_version_means_all() -> None:
    versions = [_v("0.7.0", renders=100), _v("0.6.0", renders=50)]
    s = sum_since(versions, user_version=None, weights=WEIGHTS)
    assert s == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# _best_version_token tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("s,expected", [
    ("v0.7.1", "0.7.1"),
    ("Week 3 v3.6.14", "3.6.14"),
    ("P3 Ep.8 v0.0.8.1", "0.0.8.1"),
    ("v0.80 Hotfix", "0.80"),
    ("Ch. 12 Official", None),     # no dotted-numeric token
    ("Chapter_12_Patreon", None),   # no dotted-numeric token
    (None, None),
    ("", None),
])
def test_best_version_token(s: str | None, expected: str | None) -> None:
    assert _best_version_token(s) == expected


# ---------------------------------------------------------------------------
# is_user_behind tests — must return False (no false positives)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("installed,upstream", [
    ("0.7.1", "v0.7.1"),             # Radiant: v-prefix only
    ("1.0.0", "v1.0.0"),             # Sorcerer: v-prefix only
    ("1.10a", "v1.10a"),             # Chasing Sunsets: v-prefix + alpha
    ("0.80", "v0.80 Hotfix"),        # Reunion: v-prefix + trailing text
    ("Chapter_12_Patreon", "Ch. 12 Official"),  # Power Vacuum: no numeric tokens
    (None, "v1.0"),                  # no installed version
    ("1.0", None),                   # no upstream version
    ("2.0", "v1.0"),                 # user is AHEAD, not behind
])
def test_is_user_behind_false(installed: str | None, upstream: str | None) -> None:
    assert is_user_behind(installed, upstream) is False


# ---------------------------------------------------------------------------
# is_user_behind tests — must return True (genuine updates still surface)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("installed,upstream", [
    ("0.20.1", "v0.20.2 Extra"),         # Photo Hunt: minor version bump
    ("1.15.0", "1.16.0"),                # Bright Lord: no prefix, numeric bump
    ("3.6.9", "Week 3 v3.6.14"),         # Fetish Locator: embedded version
    ("0.0.8", "P3 Ep.8 v0.0.8.1"),      # Thief of Hearts: extra patch segment
])
def test_is_user_behind_true(installed: str | None, upstream: str | None) -> None:
    assert is_user_behind(installed, upstream) is True


# ---------------------------------------------------------------------------
# version_strictly_after tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("candidate,baseline", [
    ("v.0.2.6f", "v0.2.1p"),     # messy label: 0.2.6 > 0.2.1
    ("0.10.0", "0.9.0"),          # major digit difference
    ("0.4.7", "0.4.5"),           # patch bump
])
def test_version_strictly_after_true(candidate: str, baseline: str) -> None:
    assert version_strictly_after(candidate, baseline) is True


@pytest.mark.parametrize("candidate,baseline", [
    ("v.0.2.0p", "v0.2.1p"),      # older
    ("0.9.0", "0.9.0"),            # equal
    ("Chapter_12", None),           # no dotted token on candidate side
    (None, "0.1"),                  # None candidate
    ("garbage", "0.1"),             # no parseable dotted token
])
def test_version_strictly_after_false(candidate: str | None, baseline: str | None) -> None:
    assert version_strictly_after(candidate, baseline) is False


def test_sum_since_messy_label_regression() -> None:
    """v.0.2.6f (parses to 0.2.6) must be counted as newer than user v0.2.1p."""
    weights = MagnitudeScoreConfig()
    versions = [
        _v("v.0.2.6f", renders=50),   # newer — should be counted
        _v("v0.2.0p", renders=200),    # older — should be excluded
    ]
    score = sum_since(versions, user_version="v0.2.1p", weights=weights)
    assert score == pytest.approx(0.05)


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
    assert res.basis == "version match"
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
    assert res.basis == "no figures listed"
    assert _counted_versions(res) == ["0.7.0"]


def test_resolve_messy_label_floor_medium() -> None:
    versions = [_v("v.0.2.6f", renders=50), _v("v0.2.0p", renders=5)]
    res = resolve_baseline(versions, "v0.2.1p")
    assert res.confidence == "medium"
    assert res.anchor == "v0.2.0p"
    assert _counted_versions(res) == ["v.0.2.6f"]


def test_resolve_prefers_normalized_version_for_range_label() -> None:
    # "v0.9.2-5" is a range covering up to 0.9.5; the LLM normalizes it. The raw
    # parser reads "0.9.2" and would wrongly drop it for an installed 0.9.4.
    versions = [
        _v("0.9.2-5", version_normalized="0.9.5",
           summary_one_line="bugfixes + a few renders"),
        _v("0.9.0", version_normalized="0.9.0", renders=2050),
    ]
    res = resolve_baseline(versions, "0.9.4")
    # Counted because the normalized 0.9.5 > installed 0.9.4 (raw label would not).
    assert _counted_versions(res) == ["0.9.2-5"]


def test_resolve_falls_back_to_raw_label_when_normalized_absent_or_bad() -> None:
    # No version_normalized → parse the raw label.
    a = resolve_baseline([_v("0.7.0", renders=10), _v("0.6.0", renders=5)], "0.6.0")
    assert _counted_versions(a) == ["0.7.0"]
    # Unusable version_normalized (no dotted token) → fall back to raw label.
    b = resolve_baseline(
        [_v("0.7.0", version_normalized="garbage", renders=10),
         _v("0.6.0", renders=5)],
        "0.6.0",
    )
    assert _counted_versions(b) == ["0.7.0"]


def test_changelog_behind_upstream_detects_missing_latest_notes() -> None:
    # Changelog tops out at 0.9; F95 reports 0.10 available -> notes missing.
    versions = [_v("0.9", renders=100), _v("0.8", renders=50)]
    assert changelog_behind_upstream(versions, "v0.10") is True


def test_changelog_behind_upstream_false_when_changelog_covers_upstream() -> None:
    versions = [_v("0.9.5", renders=10), _v("0.9.0", renders=5)]
    assert changelog_behind_upstream(versions, "v0.9.5 Public") is False


def test_changelog_behind_upstream_uses_normalized_range_label() -> None:
    # Eternum: "0.9.2-5" normalizes to 0.9.5, matching upstream 0.9.5 -> not stale.
    versions = [_v("0.9.2-5", version_normalized="0.9.5"),
                _v("0.9.0", version_normalized="0.9.0")]
    assert changelog_behind_upstream(versions, "v0.9.5 Public") is False


def test_changelog_behind_upstream_conservative_when_unparseable() -> None:
    assert changelog_behind_upstream([_v("0.9", renders=1)], None) is False
    assert changelog_behind_upstream([_v("0.9", renders=1)], "Chapter 12") is False
    assert changelog_behind_upstream([], "0.10") is False


def test_resolve_every_branch_has_a_basis() -> None:
    # Every resolution must carry a short, non-empty basis for the Accuracy
    # column. Exercise each branch.
    blocks = [_v("0.2.0", renders=20), _v("0.1.0", renders=10)]
    scenarios = [
        resolve_baseline(blocks, None),                       # never played
        resolve_baseline(blocks, "0.2.0"),                    # exact
        resolve_baseline([_v("0.2.0", renders=1), _v("0.1.6", renders=1),
                          _v("0.1.4", renders=1)], "0.1.5"),  # floor
        resolve_baseline(blocks, "0.0.9"),                    # behind all
        resolve_baseline([_v("0.2.0", renders=20)], "0.1.0"), # single block
        resolve_baseline(blocks, "Chapter_12"),               # unparseable
        resolve_baseline([_v("0.7.0"), _v("0.6.0", renders=1)], "0.6.0"),  # all-null
    ]
    for res in scenarios:
        assert res.basis, "every resolution should carry a basis label"
