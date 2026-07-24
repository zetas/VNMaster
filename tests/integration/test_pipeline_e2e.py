from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from vnmaster.config import Config
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, DigestRun, LibraryGame
from vnmaster.llm.changelog import ExtractionResult
from vnmaster.pipeline import PipelineDeps, run_digest_pipeline


@pytest.mark.asyncio
async def test_pipeline_end_to_end_creates_digest_run(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Build a real vnmaster.db
    db_path = tmp_path / "vnmaster.db"
    engine = create_engine_for(db_path)
    Base.metadata.create_all(engine)

    # Fake F95Checker DB reader returning one game
    # Note: SimpleNamespace is used instead of MagicMock because MagicMock(name=...)
    # sets the mock's display name, making .name return a child Mock rather than the string.
    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=None)
    f95_db.iter_all_games = MagicMock(return_value=iter([
        SimpleNamespace(
            id=42, name="Eternum", version="0.7.0", developer="Caribdis",
            engine="Ren'Py", status="ongoing", last_updated=100, changelog="v0.7\n- 800 renders",
            description="Sci-fi", image_url=None, tags=["corruption"],
            executable=None, archived=0, rating=4,
        )
    ]))

    # Empty filesystem scanners → user has nothing installed/played
    play_scanner = MagicMock(return_value=[])
    disk_scanner = MagicMock(return_value=[])

    # Mock LLM cache (returns regex-shaped output)
    llm_cache = MagicMock()
    llm_cache.extract_for.return_value = ExtractionResult(
        method="llm",
        versions=[{"version": "0.7.0", "renders": 800, "bugfix_only": False,
                   "summary_one_line": "Maya story"}],
        cost_usd=0.005,
    )

    # Webhook + bot mocks
    # Use side_effect so each send call returns a distinct message ID, avoiding
    # UNIQUE constraint violations on digest_entries(discord_message_id, embed_index).
    _msg_ids = iter(["m0", "m1", "m2", "m3", "m4"])
    webhook = AsyncMock()
    webhook.send.side_effect = lambda *a, **kw: MagicMock(id=next(_msg_ids))
    bot = MagicMock()
    bot.add_reaction = AsyncMock()

    cfg = Config.load(
        Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml"
    )

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db, scan_play_history=play_scanner,
        scan_disk=disk_scanner,
        llm_cache=llm_cache, webhook=webhook, bot=bot,
        config=cfg, now_epoch=1_000, channel_id="c-1",
    )
    await run_digest_pipeline(deps)

    with session_scope(engine) as s:
        runs = s.execute(select(DigestRun)).scalars().all()
        assert len(runs) == 1
        assert runs[0].llm_cost_usd > 0

        library = s.execute(select(LibraryGame)).scalars().all()
        assert len(library) == 1
        assert library[0].game_title == "Eternum"

    # At least one webhook send happened (kickoff + at least one embed)
    assert webhook.send.await_count >= 2


@pytest.mark.asyncio
async def test_pipeline_persists_digest_entries_even_if_row_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a missing library row must not roll back the
    digest_run + digest_entries write. Otherwise the bot can't route reactions
    on messages that did get posted to Discord.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=None)
    f95_db.iter_all_games = MagicMock(return_value=iter([]))

    play_scanner = MagicMock(return_value=[])
    disk_scanner = MagicMock(return_value=[])
    llm_cache = MagicMock()

    # Pre-seed digest infrastructure: add a library row with an
    # upstream_last_updated_at newer than previous_digest_run_at=0, so it
    # appears as an update candidate. Then DELETE it before the post-stamp.
    from vnmaster.db.models import LibraryGame as LG
    with session_scope(engine) as s:
        s.add(LG(
            f95_thread_id=42, game_title="Ghost", installed_version="0.5",
            latest_upstream_version="0.7", upstream_last_updated_at=500,
            raw_changelog="", created_at=1, updated_at=1,
        ))

    # Intercept select_digest_candidates? Easier: use a webhook whose send
    # triggers a side-effect that deletes the row, simulating a race condition.
    _calls = {"n": 0}

    def _send_side_effect(*a, **kw):
        _calls["n"] += 1
        # On the 2nd send (the update embed), delete the library row.
        if _calls["n"] == 2:
            with session_scope(engine) as s:
                row = s.execute(
                    select(LG).where(LG.f95_thread_id == 42)
                ).scalar_one()
                s.delete(row)
        return MagicMock(id=f"m{_calls['n']}")

    webhook = AsyncMock()
    webhook.send.side_effect = _send_side_effect
    bot = MagicMock()
    bot.add_reaction = AsyncMock()

    llm_cache.extract_for.return_value = ExtractionResult(
        method="llm",
        versions=[{"version": "0.7", "renders": 100, "bugfix_only": False,
                   "summary_one_line": "x"}],
        cost_usd=0.001,
    )

    cfg = Config.load(
        Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml"
    )
    deps = PipelineDeps(
        engine=engine, f95_db=f95_db, scan_play_history=play_scanner,
        scan_disk=disk_scanner,
        llm_cache=llm_cache, webhook=webhook, bot=bot,
        config=cfg, now_epoch=1_000, channel_id="c-1",
    )
    await run_digest_pipeline(deps)

    # DigestRun and DigestEntry must still be persisted despite the missing row.
    with session_scope(engine) as s:
        from vnmaster.db.models import DigestEntry
        runs = s.execute(select(DigestRun)).scalars().all()
        assert len(runs) == 1, "DigestRun rolled back — bug regressed"
        entries = s.execute(select(DigestEntry)).scalars().all()
        assert len(entries) == 1, "DigestEntry rolled back — reactions would silently fail"
        assert entries[0].discord_message_id == "m2"


@pytest.mark.asyncio
async def test_daily_mode_silent_when_no_new_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    # Seed one game already at its notified version → no daily signal.
    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.7.0",
            latest_upstream_version="0.7.0", last_daily_notified_version="0.7.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=None)
    f95_db.iter_all_games = MagicMock(return_value=iter([
        SimpleNamespace(
            id=42, name="Eternum", version="0.7.0", developer="Caribdis",
            engine="Ren'Py", status="1", last_updated=100, changelog="notes",
            description="", image_url=None, tags=[], executable=None,
            archived=0, rating=4,
        )
    ]))
    llm_cache = MagicMock()
    webhook = AsyncMock()
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=llm_cache, webhook=webhook, bot=bot, config=cfg,
        now_epoch=1000, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    assert webhook.send.await_count == 0          # nothing posted
    assert llm_cache.extract_for.call_count == 0  # no LLM calls
    with session_scope(engine) as s:
        assert s.execute(select(DigestRun)).scalars().all() == []


@pytest.mark.asyncio
async def test_daily_mode_alerts_and_sets_watermark_without_touching_weekly_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.5.0",
            latest_upstream_version="0.5.0", last_daily_notified_version="0.5.0",
            last_daily_notified_status="1", status="1",
            last_seen_in_digest_at=None, created_at=1, updated_at=1,
        ))

    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=None)
    f95_db.iter_all_games = MagicMock(return_value=iter([
        SimpleNamespace(
            id=42, name="Eternum", version="0.7.0", developer="Caribdis",
            engine="Ren'Py", status="1", last_updated=300, changelog="v0.7 notes",
            description="", image_url=None, tags=[], executable=None,
            archived=0, rating=4,
        )
    ]))
    from vnmaster.llm.changelog import ExtractionResult
    llm_cache = MagicMock()
    llm_cache.extract_for.return_value = ExtractionResult(
        method="llm",
        versions=[{"version": "0.7.0", "renders": 400, "bugfix_only": False,
                   "summary_one_line": "story"}],
        cost_usd=0.004,
    )
    _msg_ids = iter(["m0", "m1", "m2"])
    webhook = AsyncMock()
    webhook.send.side_effect = lambda *a, **kw: MagicMock(id=next(_msg_ids))
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=llm_cache, webhook=webhook, bot=bot, config=cfg,
        now_epoch=1000, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    # Kickoff mentions @everyone.
    first_call = webhook.send.call_args_list[0]
    assert "@everyone" in (first_call.kwargs.get("content") or "")

    with session_scope(engine) as s:
        g = s.get(LibraryGame, 42)
        assert g.last_daily_notified_version == "0.7.0"   # watermark advanced
        assert g.last_seen_in_digest_at is None            # weekly throttle untouched
        runs = s.execute(select(DigestRun)).scalars().all()
        assert len(runs) == 1 and runs[0].kind == "daily"


@pytest.mark.asyncio
async def test_daily_insert_of_new_game_baselines_status_so_weekly_shows_no_spurious_callout(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a game first discovered during a *daily* run must have its
    `status` baselined on insert (not left NULL), so a following *weekly* run
    doesn't misread "never had a status" as "status changed" and render a
    spurious Now-completed/on-hold/abandoned callout for a game that was
    merely newly discovered, not actually transitioned.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    # Start from a completely empty library — no seeded LibraryGame rows.

    def make_f95_row(last_updated: int):
        return SimpleNamespace(
            id=42, name="Eternum", version="0.7.0", developer="Caribdis",
            engine="Ren'Py", status="2", last_updated=last_updated,
            changelog="v0.7 -- the end", description="", image_url=None,
            tags=[], executable=None, archived=0, rating=4,
        )

    # --- Daily run: discovers the game for the first time ---
    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=None)
    f95_db.iter_all_games = MagicMock(return_value=iter([make_f95_row(100)]))

    llm_cache = MagicMock()
    llm_cache.extract_for.return_value = ExtractionResult(
        method="llm",
        versions=[{"version": "0.7.0", "renders": 0, "bugfix_only": False,
                   "summary_one_line": "complete"}],
        cost_usd=0.001,
    )
    _daily_msg_ids = iter(["d0", "d1", "d2"])
    webhook = AsyncMock()
    webhook.send.side_effect = lambda *a, **kw: MagicMock(id=next(_daily_msg_ids))
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    daily_deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=llm_cache, webhook=webhook, bot=bot, config=cfg,
        now_epoch=1000, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(daily_deps)

    # A brand-new game discovered on a daily run is correctly NOT alerted on
    # (the insert baselines the watermark) -- don't assert on the webhook here.
    with session_scope(engine) as s:
        g = s.get(LibraryGame, 42)
        assert g is not None
        assert g.status == "2", "daily insert must baseline status, not leave it NULL"
        assert g.status_changed == 0

    # --- Weekly run: same game, same status -- must not report a transition ---
    f95_db_weekly = MagicMock()
    f95_db_weekly.check_schema = MagicMock()
    f95_db_weekly.last_successful_refresh_epoch = MagicMock(return_value=None)
    f95_db_weekly.iter_all_games = MagicMock(return_value=iter([make_f95_row(200)]))

    llm_cache_weekly = MagicMock()
    llm_cache_weekly.extract_for.return_value = ExtractionResult(
        method="llm",
        versions=[{"version": "0.7.0", "renders": 0, "bugfix_only": False,
                   "summary_one_line": "complete"}],
        cost_usd=0.001,
    )
    _weekly_msg_ids = iter(["w0", "w1", "w2"])
    webhook_weekly = AsyncMock()
    webhook_weekly.send.side_effect = lambda *a, **kw: MagicMock(id=next(_weekly_msg_ids))
    bot_weekly = MagicMock()
    bot_weekly.add_reaction = AsyncMock()

    weekly_deps = PipelineDeps(
        engine=engine, f95_db=f95_db_weekly,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=llm_cache_weekly, webhook=webhook_weekly, bot=bot_weekly, config=cfg,
        now_epoch=100_000, channel_id="c-1", mode="weekly",
    )
    await run_digest_pipeline(weekly_deps)

    with session_scope(engine) as s:
        g = s.get(LibraryGame, 42)
        assert g is not None
        assert g.status_changed == 0, (
            "weekly run must not flag a status transition for a game that was "
            "only just discovered by the daily run, not actually transitioned"
        )


def _stale_f95_db(rows, *, last_refresh):
    f95_db = MagicMock()
    f95_db.check_schema = MagicMock()
    f95_db.iter_all_games = MagicMock(return_value=iter(rows))
    f95_db.last_successful_refresh_epoch = MagicMock(return_value=last_refresh)
    return f95_db


def _eternum_row(version="0.7.0", status="1", last_updated=100):
    return SimpleNamespace(
        id=42, name="Eternum", version=version, developer="Caribdis",
        engine="Ren'Py", status=status, last_updated=last_updated,
        changelog="notes", description="", image_url=None, tags=[],
        executable=None, archived=0, rating=4,
    )


@pytest.mark.asyncio
async def test_daily_stale_and_quiet_posts_standalone_warning(tmp_path, monkeypatch):
    """A stale db on a no-updates night must warn instead of staying silent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.7.0",
            latest_upstream_version="0.7.0", last_daily_notified_version="0.7.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    now = 1_784_100_000
    f95_db = _stale_f95_db([_eternum_row()], last_refresh=now - 3 * 86400)
    webhook = AsyncMock()
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=MagicMock(), webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    assert webhook.send.await_count == 1
    content = webhook.send.call_args.kwargs.get("content") or ""
    assert content.startswith("@everyone F95Checker data is stale")
    assert "3 days ago" in content
    with session_scope(engine) as s:
        assert s.execute(select(DigestRun)).scalars().all() == []


@pytest.mark.asyncio
async def test_daily_fresh_and_quiet_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.7.0",
            latest_upstream_version="0.7.0", last_daily_notified_version="0.7.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    now = 1_784_100_000
    f95_db = _stale_f95_db([_eternum_row()], last_refresh=now - 3600)
    webhook = AsyncMock()
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=MagicMock(), webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    assert webhook.send.await_count == 0


@pytest.mark.asyncio
async def test_daily_stale_with_updates_appends_warning_without_second_ping(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=42, game_title="Eternum", installed_version="0.5.0",
            latest_upstream_version="0.5.0", last_daily_notified_version="0.5.0",
            last_daily_notified_status="1", status="1",
            created_at=1, updated_at=1,
        ))

    now = 1_784_100_000
    f95_db = _stale_f95_db(
        [_eternum_row(last_updated=300)], last_refresh=now - 3 * 86400
    )
    llm_cache = MagicMock()
    llm_cache.extract_for.return_value = ExtractionResult(
        method="llm",
        versions=[{"version": "0.7.0", "renders": 400, "bugfix_only": False,
                   "summary_one_line": "story"}],
        cost_usd=0.004,
    )
    _msg_ids = iter(["m0", "m1", "m2"])
    webhook = AsyncMock()
    webhook.send.side_effect = lambda *a, **kw: MagicMock(id=next(_msg_ids))
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=llm_cache, webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="daily",
    )
    await run_digest_pipeline(deps)

    kickoff = webhook.send.call_args_list[0].kwargs.get("content") or ""
    assert "F95Checker data is stale" in kickoff
    # The daily kickoff already pings; the warning line must not add another.
    assert kickoff.count("@everyone") == 1


@pytest.mark.asyncio
async def test_weekly_stale_appends_warning_with_ping(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = create_engine_for(tmp_path / "vnmaster.db")
    Base.metadata.create_all(engine)

    now = 1_784_100_000
    f95_db = _stale_f95_db([], last_refresh=now - 3 * 86400)
    webhook = AsyncMock()
    webhook.send.side_effect = lambda *a, **kw: MagicMock(id="m0")
    bot = MagicMock()
    bot.add_reaction = AsyncMock()
    cfg = Config.load(Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml")

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db,
        scan_play_history=MagicMock(return_value=[]),
        scan_disk=MagicMock(return_value=[]),
        llm_cache=MagicMock(), webhook=webhook, bot=bot, config=cfg,
        now_epoch=now, channel_id="c-1", mode="weekly",
    )
    await run_digest_pipeline(deps)

    kickoff = webhook.send.call_args_list[0].kwargs.get("content") or ""
    assert kickoff.startswith("Weekly digest")
    assert "@everyone F95Checker data is stale" in kickoff
