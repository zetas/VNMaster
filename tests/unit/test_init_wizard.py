from types import SimpleNamespace
from pathlib import Path
import tomllib

from vnmaster.f95_search import F95SearchHit
from vnmaster.init_wizard import (
    _build_candidate_rows,
    _save_credentials_early,
    _save_dir_to_search_term,
    generate_import_candidates,
)
from vnmaster.paths import VNMasterPaths


def _ph(name: str):
    return SimpleNamespace(save_dir_name=name)


def _ig(name: str):
    return SimpleNamespace(folder_name=name)


def _f95(id_: int, name: str):
    return SimpleNamespace(id=id_, name=name)


def _no_search(_q):
    """Search stub that returns nothing — for tests that don't care about F95Zone."""
    return []


def test_save_credentials_keeps_webhook_only_in_private_secrets(
    tmp_path: Path,
) -> None:
    paths = VNMasterPaths(
        games_root=tmp_path / "Games",
        renpy_saves_root=tmp_path / "RenPy",
        f95checker_db=tmp_path / "f95.db",
        vnmaster_db=tmp_path / "vnmaster.db",
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
    )

    _save_credentials_early(
        paths=paths,
        anthropic_key="anthropic-test-value",
        discord_token="discord-test-value",
        discord_webhook_url="https://example.invalid/test-webhook",
        f95zone_cookies="xf_user=fake; xf_session=fake",
        cfg_paths_section={
            "games_root": "~/Games",
            "renpy_saves_root": "~/Library/RenPy",
            "f95checker_db": "~/f95.db",
            "vnmaster_db": "~/vnmaster.db",
        },
        cfg_discord_section={"guild_id": "123", "channel_id": "456"},
        existing_config={},
    )

    config_path = paths.config_dir / "config.toml"
    secrets_path = paths.config_dir / "secrets.toml"
    config = tomllib.loads(config_path.read_text())
    secrets = tomllib.loads(secrets_path.read_text())

    assert "webhook_url" not in config["discord"]
    assert secrets["discord_webhook_url"] == "https://example.invalid/test-webhook"
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert secrets_path.stat().st_mode & 0o777 == 0o600


def test_generate_candidates_orders_by_confidence_then_none_last() -> None:
    candidates = [
        ("none", "Mystery", "no match", "https://f95zone.to/search/?q=mystery"),
        ("low", "ObscureGame", "weak match", "https://f95zone.to/threads/.44/"),
        ("high", "Eternum", "score 95", "https://f95zone.to/threads/.42/"),
        ("medium", "MyGame", "score 78", "https://f95zone.to/threads/.43/"),
    ]
    out = generate_import_candidates(candidates)
    body = out.split("\n", 5)[5]  # skip the header preamble lines
    order_positions = [
        body.index("Eternum"),
        body.index("MyGame"),
        body.index("ObscureGame"),
        body.index("Mystery"),
    ]
    assert order_positions == sorted(order_positions), order_positions


def test_blank_candidates_returns_header_only() -> None:
    out = generate_import_candidates([])
    assert "no candidates" in out.lower()


def test_save_dir_strips_trailing_timestamp() -> None:
    assert _save_dir_to_search_term("Eternum-1610153667") == "Eternum"
    # camelCase split: ATouchofMagic → "A Touchof Magic" → wordsegment
    # expands "touchof" → "touch of" → stop-word strip drops "A" and "of"
    # → "Touch Magic" (verified: finds #182184 "A Touch of Magic")
    assert "Magic" in _save_dir_to_search_term("ATouchofMagic-1691411418")
    assert "Touch" in _save_dir_to_search_term("ATouchofMagic-1691411418")


def test_save_dir_segments_concatenated_lowercase() -> None:
    """Save dirs like 'mybrotherswife' have no case or separator boundaries —
    we use wordsegment to split them into English words before searching."""
    assert _save_dir_to_search_term("Mybrotherswife-1602883358") == "my brothers wife"
    assert _save_dir_to_search_term("sixteenyearslater-1627631125") == "sixteen years later"
    assert _save_dir_to_search_term("mydemonicromance-1610953985") == "my demonic romance"


def test_save_dir_preserves_real_single_words() -> None:
    """Wordsegment naively splits 'Despondence' into 'de spondence'. The
    3+ segments threshold prevents that from corrupting real English words."""
    # "Of Devotion and Despondence" — "Devotion" and "Despondence" must
    # stay intact (only stripping "of", "and" stop words).
    assert _save_dir_to_search_term("Of Devotion and Despondence") == "Devotion Despondence"


def test_save_dir_strips_stop_words() -> None:
    """The latest_alpha API drops stop words in titles but requires them in
    queries — so we must strip them client-side to find anything."""
    assert _save_dir_to_search_term("Of Devotion and Despondence") == "Devotion Despondence"
    # OneDayAtATime → "One Day At A Time" → strip stop words → "One Day Time"
    # (verified live: this query finds #67104 "One Day at a Time")
    assert _save_dir_to_search_term("OneDayAtATime-1599887994") == "One Day Time"


def test_save_dir_leaves_non_timestamp_alone() -> None:
    # camelCase split is intentional — better search hits for compound titles.
    assert _save_dir_to_search_term("FetishLocator-Week2") == "Fetish Locator Week2"
    assert _save_dir_to_search_term("DR2") == "DR2"
    assert _save_dir_to_search_term("0x52-URM") == "0x52 URM"


def test_save_dir_strips_version_suffix() -> None:
    assert _save_dir_to_search_term("GameName-1.2.3") == "Game Name"
    assert _save_dir_to_search_term("GameName-pc") == "Game Name"


def test_save_dir_falls_back_when_only_stop_words() -> None:
    """If stripping leaves nothing, return the un-stripped tokens instead of ''."""
    # "Of And The" → all stop words; result must be non-empty
    result = _save_dir_to_search_term("Of And The")
    assert result.strip() != ""


def test_build_candidate_rows_skips_renpy_engine_dirs() -> None:
    """Ren'Py creates 'persistent', 'tokens', 'tutorial-N', 'launcher-N'
    directories under ~/Library/RenPy/ that aren't games. Don't waste a
    search request on them."""
    rows = _build_candidate_rows(
        play_history=[
            _ph("persistent"), _ph("tokens"), _ph("tutorial-7"),
            _ph("launcher-4"), _ph("Eternum-1234567890"),
        ],
        installed=[],
        f95_rows=[],
        search_fn=_no_search,
    )
    names = {r[1] for r in rows}
    assert "persistent" not in names
    assert "tokens" not in names
    assert "tutorial-7" not in names
    assert "launcher-4" not in names
    assert "Eternum-1234567890" in names


def test_build_candidate_rows_uses_local_match_when_high_confidence() -> None:
    """If F95Checker already tracks the game with a HIGH score, no search needed."""
    search_called = []

    def stub_search(q):
        search_called.append(q)
        return []

    rows = _build_candidate_rows(
        play_history=[_ph("Eternum-1610153667")],
        installed=[],
        f95_rows=[_f95(42, "Eternum")],
        search_fn=stub_search,
    )
    assert len(rows) == 1
    conf, _name, _hint, url = rows[0]
    assert conf == "high"
    assert "42" in url
    # Local match short-circuits the search call.
    assert search_called == []


def test_build_candidate_rows_searches_f95zone_when_local_fails() -> None:
    """When F95Checker has no good local match, fall through to F95Zone search."""

    def stub_search(q):
        # Pretend F95Zone returned a perfect hit for "Touch of Magic"
        return [F95SearchHit(
            title="A Touch of Magic [v1.0]",
            thread_id=42000,
            url="https://f95zone.to/threads/.42000/",
        )]

    rows = _build_candidate_rows(
        play_history=[_ph("ATouchofMagic-1691411418")],
        installed=[],
        f95_rows=[_f95(99, "Eternum")],  # unrelated local entry
        search_fn=stub_search,
    )
    assert len(rows) == 1
    conf, _name, _hint, url = rows[0]
    assert conf in ("high", "medium")
    assert "42000" in url


def test_build_candidate_rows_routes_empty_search_to_none() -> None:
    """No F95Zone hits → emit a manual search URL with confidence NONE."""
    rows = _build_candidate_rows(
        play_history=[_ph("DR2")],
        installed=[],
        f95_rows=[],
        search_fn=_no_search,
    )
    assert len(rows) == 1
    conf, _name, _hint, url = rows[0]
    assert conf == "none"
    assert url and "search" in url.lower()


def test_build_candidate_rows_routes_low_score_search_to_none() -> None:
    """If F95Zone's top hit doesn't actually look like our game, emit NONE."""

    def stub_search(q):
        return [F95SearchHit(
            title="Completely Unrelated Different Title XYZ",
            thread_id=99999,
            url="https://f95zone.to/threads/.99999/",
        )]

    rows = _build_candidate_rows(
        play_history=[_ph("MyGameName-1691411418")],
        installed=[],
        f95_rows=[],
        search_fn=stub_search,
    )
    conf, _name, _hint, url = rows[0]
    assert conf == "none", "low-similarity top hit should not be auto-accepted"
    assert "search" in url.lower()


def test_build_candidate_rows_falls_back_to_search_url_when_search_raises() -> None:
    """If F95Zone search throws (network error, HTTP 5xx), don't crash."""

    def raising_search(q):
        raise RuntimeError("network down")

    rows = _build_candidate_rows(
        play_history=[_ph("SomeGame-1691411418")],
        installed=[],
        f95_rows=[],
        search_fn=raising_search,
    )
    conf, _name, hint, url = rows[0]
    assert conf == "none"
    assert "failed" in hint.lower()
    assert url and "search" in url.lower()


def test_build_candidate_rows_trips_circuit_after_3_consecutive_failures() -> None:
    """When F95Zone rate-limits, every remaining search will fail. Don't
    burn time attempting all 67; trip the circuit after 3 failures and
    route the rest straight to NONE without trying."""
    call_count = {"n": 0}

    def always_fails(q):
        call_count["n"] += 1
        raise RuntimeError("rate limited")

    rows = _build_candidate_rows(
        play_history=[_ph(f"Game{i}-1234567890") for i in range(10)],
        installed=[],
        f95_rows=[],
        search_fn=always_fails,
    )
    # Only the first 3 entries triggered actual search calls; the rest
    # short-circuited to NONE without ever calling the search function.
    assert call_count["n"] == 3, (
        f"expected exactly 3 search attempts before circuit trips, "
        f"got {call_count['n']}"
    )
    # All 10 entries should still be present in the output.
    assert len(rows) == 10
    # The post-circuit-trip rows should mention "circuit" or "rate limit".
    later_hints = [r[2] for r in rows[3:]]
    assert any("circuit" in h.lower() or "rate" in h.lower() for h in later_hints)


def test_build_candidate_rows_progress_callback_fires_per_entry() -> None:
    calls = []

    def cb(idx, total, name):
        calls.append((idx, total, name))

    _build_candidate_rows(
        play_history=[_ph("A"), _ph("B"), _ph("C")],
        installed=[],
        f95_rows=[],
        search_fn=_no_search,
        progress_cb=cb,
    )
    assert calls == [(1, 3, "A"), (2, 3, "B"), (3, 3, "C")]


def test_build_candidate_rows_dedupes_seen_names() -> None:
    rows = _build_candidate_rows(
        play_history=[_ph("Foo"), _ph("Foo")],
        installed=[],
        f95_rows=[],
        search_fn=_no_search,
    )
    assert len(rows) == 1


def test_build_candidate_rows_skips_live_search_without_cookies() -> None:
    """Without cookies AND no test stub, the wizard must not attempt a live
    F95Zone search (which would 403 and waste 0.6s per entry on backoff)."""
    # No search_fn → would normally trigger a real network call. With
    # cookie_header=None and no search_fn override, search should be skipped.
    rows = _build_candidate_rows(
        play_history=[_ph("MysteryGame-1234567890")],
        installed=[],
        f95_rows=[],
        # search_fn omitted on purpose; cookie_header omitted (defaults to None)
    )
    assert len(rows) == 1
    conf, _name, hint, url = rows[0]
    assert conf == "none"
    assert "no F95Zone cookies" in hint or "manual search" in hint.lower()
    assert url and "search" in url.lower()


def test_build_candidate_rows_uses_search_fn_even_without_cookies() -> None:
    """If a search_fn is injected (as in tests), it runs regardless of cookies."""

    def stub(q):
        return [F95SearchHit(
            title="My Game", thread_id=777,
            url="https://f95zone.to/threads/.777/",
        )]

    rows = _build_candidate_rows(
        play_history=[_ph("MyGame-1234567890")],
        installed=[],
        f95_rows=[],
        search_fn=stub,
    )
    conf, *_ = rows[0]
    assert conf in ("high", "medium", "low")


def test_build_candidate_rows_strips_bracket_tags_before_scoring() -> None:
    """F95 thread titles include version + dev decorations: 'Eternum [v0.7] [Caribdis]'.
    The scorer must compare clean title 'Eternum' against the search term
    'Eternum', not the bracketed string — otherwise it scores ~50 and lands in NONE.
    """

    def stub(q):
        return [F95SearchHit(
            title="Eternum [v0.7.5] [Caribdis]",
            thread_id=93340,
            url="https://f95zone.to/threads/.93340/",
        )]

    rows = _build_candidate_rows(
        play_history=[_ph("Eternum-1610153667")],
        installed=[],
        f95_rows=[],
        search_fn=stub,
    )
    conf, _name, _hint, url = rows[0]
    assert conf == "high", "bracket-stripped score should be >=85"
    assert "93340" in url


def test_build_candidate_rows_picks_best_of_top_hits_not_just_first() -> None:
    """F95Zone may put mods or walkthroughs above the canonical thread.
    We must score every hit and pick the best match.
    """

    def stub(q):
        return [
            F95SearchHit(
                title="Eternum - Walkthrough Mod",
                thread_id=11111,
                url="https://f95zone.to/threads/.11111/",
            ),
            F95SearchHit(
                title="Eternum [v0.7.5] [Caribdis]",
                thread_id=93340,
                url="https://f95zone.to/threads/.93340/",
            ),
        ]

    rows = _build_candidate_rows(
        play_history=[_ph("Eternum-1610153667")],
        installed=[],
        f95_rows=[],
        search_fn=stub,
    )
    conf, _name, _hint, url = rows[0]
    # Best match should be the canonical thread (93340), not the mod (11111).
    assert "93340" in url
    assert conf == "high"


def test_daily_plist_renders_for_wizard_default() -> None:
    from pathlib import Path
    from vnmaster.launchd import render_daily_plist
    out = render_daily_plist(
        bin_path=Path("/opt/bin/vnmaster"), log_dir=Path("/L"), cron="0 1 * * *"
    )
    assert "dev.vnmaster.daily" in out
    assert "--daily" in out
