from vnmaster.digest.embeds import build_update_embed
from vnmaster.digest.select import SelectedUpdate


def test_update_embed_includes_version_delta_and_magnitude_stars() -> None:
    sel = SelectedUpdate(
        f95_thread_id=42, game_title="Eternum",
        installed_version="0.5.2", latest_upstream_version="0.7.0",
        upstream_last_updated_at=1700_000_000, raw_changelog="", developer="Caribdis",
        image_url="http://example.com/cover.png", upstream_thread_url="http://f95/42",
        last_played_at=1690_000_000, install_path=None, tags_json='["corruption"]',
    )
    embed = build_update_embed(
        sel,
        deltas={"renders": 800, "animations": 35, "words": 12000, "scenes": 3,
                "new_locations": 1, "new_characters": 1},
        magnitude_score=5.0,  # ~5 hours of added content → 5 stars
        summary_one_line="Maya story + university chapter",
        confidence="high",
        accuracy_basis="version match",
    )
    assert "Eternum" in embed["title"]
    assert "0.7.0" in embed["title"]
    assert "0.5.2" in embed["title"]
    assert embed["color"] == 0xF0B232
    # Playtime field is a plain rating — no confidence text mixed in.
    field = next(f for f in embed["fields"] if f["name"] == "Est. added playtime")
    assert "★★★★★" in field["value"]
    assert "~5h" in field["value"]
    assert "Maya story" in field["value"]
    assert "exact" not in field["value"]  # accuracy lives in its own column
    # Confidence is a separate "Accuracy" column, expressed as a precision word.
    acc = next(f for f in embed["fields"] if f["name"] == "Accuracy")
    assert "exact" in acc["value"]
    assert "version match" in acc["value"]
    # Delta field includes humanized values
    deltas_field = next(f for f in embed["fields"] if f["name"] == "Since you last played")
    assert "+800 renders" in deltas_field["value"]
    assert "+35 animations" in deltas_field["value"]
    assert "+12k words" in deltas_field["value"]


def test_update_embed_footer_has_reaction_hints() -> None:
    """Footer includes the thread id and reaction-action hints."""
    sel = SelectedUpdate(
        f95_thread_id=42, game_title="Eternum",
        installed_version="0.5.2", latest_upstream_version="0.7.0",
        upstream_last_updated_at=1700_000_000, raw_changelog="", developer="Caribdis",
        image_url=None, upstream_thread_url="http://f95/42",
        last_played_at=1690_000_000, install_path=None, tags_json=None,
    )
    embed = build_update_embed(sel, deltas={}, magnitude_score=0.0, summary_one_line="")
    footer_text = embed["footer"]["text"]
    assert "thread #42" in footer_text
    assert "⬇️" in footer_text
    assert "DM link" in footer_text
    assert "📦" in footer_text
    assert "got it" in footer_text


def test_update_embed_uses_never_played_for_null_installed() -> None:
    sel = SelectedUpdate(
        f95_thread_id=42, game_title="X", installed_version=None,
        latest_upstream_version="0.1", upstream_last_updated_at=None,
        raw_changelog="", developer=None, image_url=None,
        upstream_thread_url=None, last_played_at=None, install_path=None,
        tags_json=None,
    )
    embed = build_update_embed(sel, deltas={}, magnitude_score=0.0, summary_one_line="")
    assert "never played" in embed["title"].lower()


def _title_for(installed: str | None, upstream: str | None) -> str:
    sel = SelectedUpdate(
        f95_thread_id=42, game_title="X", installed_version=installed,
        latest_upstream_version=upstream, upstream_last_updated_at=None,
        raw_changelog="", developer=None, image_url=None,
        upstream_thread_url=None, last_played_at=None, install_path=None,
        tags_json=None,
    )
    embed = build_update_embed(sel, deltas={}, magnitude_score=0.0, summary_one_line="")
    return embed["title"]


def test_title_does_not_double_the_v_prefix() -> None:
    """F95Zone versions usually carry their own 'v' — don't add a second one."""
    assert "vv23" not in _title_for(None, "v23")
    assert "v23" in _title_for(None, "v23")
    assert "vv" not in _title_for("v1.0", "v23")


def test_title_leaves_chapter_and_season_versions_alone() -> None:
    """Labelled versions ('Ch.4', 'S2 Ch.18') must not get a bogus 'v' glued on."""
    for upstream in ["Ch.4", "Ep.11", "S2 Ch.18", "Week 3 v3.6.14", "Part 7 v0.108"]:
        title = _title_for(None, upstream)
        assert upstream in title, title
        assert f"v{upstream}" not in title, title


def test_title_adds_v_to_bare_numeric_versions() -> None:
    """Save-file versions are usually bare numbers and still want the 'v'."""
    title = _title_for("0.5.2", "0.7.0")
    assert "v0.7.0" in title
    assert "v0.5.2" in title


def test_title_preserves_uppercase_v_prefix() -> None:
    assert "vV1.0" not in _title_for("V1.0", "v2.0")


def _field(embed: dict, name: str) -> str:
    return next(f for f in embed["fields"] if f["name"] == name)["value"]


def test_accuracy_column_uses_precision_words_not_high_low() -> None:
    sel = SelectedUpdate(
        f95_thread_id=1, game_title="X", installed_version="0.1",
        latest_upstream_version="0.2", upstream_last_updated_at=None,
        raw_changelog="", developer=None, image_url=None,
        upstream_thread_url="http://f95/1", last_played_at=None, install_path=None,
        tags_json=None,
    )
    cases = [("high", "exact"), ("medium", "≈ approx"), ("low", "rough")]
    for level, tier in cases:
        embed = build_update_embed(
            sel, deltas={}, magnitude_score=2.0, summary_one_line="",
            confidence=level, accuracy_basis="nearest version",
        )
        acc = _field(embed, "Accuracy")
        assert acc.startswith(tier)
        assert "nearest version" in acc
        # No high/low wording or emoji that could read as a magnitude scale.
        for bad in ("High", "Medium", "Low", "🟢", "🟡", "🔴"):
            assert bad not in acc
        # Rating column stays free of accuracy wording.
        assert tier not in _field(embed, "Est. added playtime")


def test_delta_field_shows_note_when_no_numeric_deltas() -> None:
    sel = SelectedUpdate(
        f95_thread_id=1, game_title="X", installed_version="0.1",
        latest_upstream_version="0.2", upstream_last_updated_at=None,
        raw_changelog="", developer=None, image_url=None,
        upstream_thread_url=None, last_played_at=None, install_path=None,
        tags_json=None,
    )
    embed = build_update_embed(
        sel, deltas={}, magnitude_score=0.0, summary_one_line="",
        no_delta_note="Bug fixes only",
    )
    value = _field(embed, "Since you last played")
    assert value == "Bug fixes only"
    assert "no structured deltas" not in value


def test_delta_field_prefers_real_deltas_over_note() -> None:
    sel = SelectedUpdate(
        f95_thread_id=1, game_title="X", installed_version="0.1",
        latest_upstream_version="0.2", upstream_last_updated_at=None,
        raw_changelog="", developer=None, image_url=None,
        upstream_thread_url=None, last_played_at=None, install_path=None,
        tags_json=None,
    )
    embed = build_update_embed(
        sel, deltas={"renders": 800}, magnitude_score=2.0, summary_one_line="",
        no_delta_note="Bug fixes only",
    )
    value = _field(embed, "Since you last played")
    assert "+800 renders" in value
    assert "Bug fixes only" not in value


def _sel(**kw) -> SelectedUpdate:
    base = dict(
        f95_thread_id=1, game_title="X", installed_version="0.1",
        latest_upstream_version="0.2", upstream_last_updated_at=None,
        raw_changelog="", developer="Dev", image_url=None,
        upstream_thread_url=None, last_played_at=None, install_path=None,
        tags_json=None,
    )
    base.update(kw)
    return SelectedUpdate(**base)


def test_status_change_shows_callout_and_tag() -> None:
    embed = build_update_embed(
        _sel(status=2, status_changed=True),
        deltas={}, magnitude_score=0.0, summary_one_line="",
    )
    assert "✅ Now completed" in embed["description"]
    assert "Completed" in embed["description"]      # standing tag too


def test_completed_standing_tag_without_callout() -> None:
    embed = build_update_embed(
        _sel(status=2, status_changed=False),
        deltas={}, magnitude_score=0.0, summary_one_line="",
    )
    assert "Completed" in embed["description"]
    assert "Now completed" not in embed["description"]  # no flip callout


def test_abandoned_callout_wording() -> None:
    embed = build_update_embed(
        _sel(status=4, status_changed=True),
        deltas={}, magnitude_score=0.0, summary_one_line="",
    )
    assert "🚫 Now abandoned" in embed["description"]


def test_ongoing_status_has_no_tag_or_install_state() -> None:
    embed = build_update_embed(
        _sel(status=1, status_changed=False),
        deltas={}, magnitude_score=0.0, summary_one_line="",
    )
    desc = embed["description"]
    assert desc == "Dev"                 # just the developer
    assert "Ren'Py" not in desc          # dropped
    assert "installed" not in desc       # dropped


def test_accuracy_column_omits_basis_line_when_empty() -> None:
    sel = SelectedUpdate(
        f95_thread_id=1, game_title="X", installed_version=None,
        latest_upstream_version="0.2", upstream_last_updated_at=None,
        raw_changelog="", developer=None, image_url=None,
        upstream_thread_url=None, last_played_at=None, install_path=None,
        tags_json=None,
    )
    embed = build_update_embed(
        sel, deltas={}, magnitude_score=2.0, summary_one_line="",
        confidence="high", accuracy_basis="",
    )
    assert _field(embed, "Accuracy") == "exact"
