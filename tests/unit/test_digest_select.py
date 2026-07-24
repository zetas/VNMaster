from pathlib import Path
from types import SimpleNamespace


from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import (
    Base, LibraryGame,
)
from vnmaster.digest.select import (
    select_digest_candidates, select_daily_candidates,
)


def _engine(tmp_path: Path):
    e = create_engine_for(tmp_path / "v.db")
    Base.metadata.create_all(e)
    return e


def _seed_library(s, **overrides) -> None:
    row = LibraryGame(
        f95_thread_id=overrides.pop("thread_id", 42),
        game_title=overrides.pop("title", "Eternum"),
        installed_version=overrides.pop("installed_version", "0.5.2"),
        latest_upstream_version=overrides.pop("latest_upstream_version", "0.7.0"),
        upstream_last_updated_at=overrides.pop("upstream_last_updated_at", 200),
        hidden=overrides.pop("hidden", 0),
        acknowledged_version=overrides.pop("acknowledged_version", None),
        last_seen_in_digest_at=overrides.pop("last_seen_in_digest_at", None),
        created_at=100, updated_at=100,
        **overrides,
    )
    s.add(row)


def _f95(thread_id=42, version="0.7.0", status="1", changelog="notes",
         last_updated=200):
    return SimpleNamespace(
        id=thread_id, version=version, status=status, last_updated=last_updated,
        changelog=changelog, developer="Caribdis", image_url=None,
        tags=["corruption"],
    )


def test_selects_recently_updated_unhidden_games(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=1, upstream_last_updated_at=300)
        _seed_library(s, thread_id=2, upstream_last_updated_at=50)  # stale
        _seed_library(s, thread_id=3, upstream_last_updated_at=400, hidden=1)
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=100, now_epoch=500,
        max_repeat_weeks=4,
    )
    ids = {u.f95_thread_id for u in result.updates}
    assert ids == {1}


def test_acknowledged_version_suppresses_updates(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=1, latest_upstream_version="0.7.0",
                      acknowledged_version="0.7.0", upstream_last_updated_at=300)
        _seed_library(s, thread_id=2, latest_upstream_version="0.7.0",
                      acknowledged_version="0.6.0", upstream_last_updated_at=300)
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=100, now_epoch=500, max_repeat_weeks=4,
    )
    ids = {u.f95_thread_id for u in result.updates}
    assert ids == {2}


def test_pre_existing_gap_within_repeat_window_is_skipped(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    one_week = 7 * 86400
    now = 100_000_000
    with session_scope(engine) as s:
        # No fresh upstream update; user is just behind. Was seen 2 weeks ago.
        _seed_library(
            s, thread_id=1,
            installed_version="0.5.0", latest_upstream_version="0.7.0",
            upstream_last_updated_at=now - 4 * one_week,
            last_seen_in_digest_at=now - 2 * one_week,
        )
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 86400, now_epoch=now,
        max_repeat_weeks=4,
    )
    assert result.updates == []


def test_pre_existing_gap_past_repeat_window_is_surfaced(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    one_week = 7 * 86400
    now = 100_000_000
    with session_scope(engine) as s:
        _seed_library(
            s, thread_id=1,
            installed_version="0.5.0", latest_upstream_version="0.7.0",
            upstream_last_updated_at=now - 10 * one_week,
            last_seen_in_digest_at=now - 5 * one_week,
        )
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 86400, now_epoch=now,
        max_repeat_weeks=4,
    )
    assert {u.f95_thread_id for u in result.updates} == {1}


def test_date_fallback_surfaces_game_played_before_upstream_update(tmp_path: Path) -> None:
    """When we don't know the played version (installed_version is None) but
    we know when the user last played, a game whose upstream was updated
    AFTER that play date should surface as a likely update."""
    engine = _engine(tmp_path)
    now = 100_000_000
    with session_scope(engine) as s:
        # No version known; last played a while ago; upstream updated after.
        _seed_library(
            s, thread_id=1,
            installed_version=None,
            last_played_at=now - 30 * 86400,
            upstream_last_updated_at=now - 5 * 86400,  # after last play
            last_seen_in_digest_at=None,
        )
        # No version; upstream updated BEFORE last play → nothing new.
        _seed_library(
            s, thread_id=2,
            installed_version=None,
            last_played_at=now - 5 * 86400,
            upstream_last_updated_at=now - 30 * 86400,  # before last play
            last_seen_in_digest_at=None,
        )
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 86400, now_epoch=now,
        max_repeat_weeks=4,
    )
    ids = {u.f95_thread_id for u in result.updates}
    assert 1 in ids
    assert 2 not in ids


def test_date_fallback_respects_repeat_window(tmp_path: Path) -> None:
    """A version-unknown game already shown within the repeat window should
    not re-surface via the date fallback."""
    engine = _engine(tmp_path)
    now = 100_000_000
    with session_scope(engine) as s:
        _seed_library(
            s, thread_id=1,
            installed_version=None,
            last_played_at=now - 30 * 86400,
            upstream_last_updated_at=now - 5 * 86400,
            last_seen_in_digest_at=now - 7 * 86400,  # shown 1 week ago
        )
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 86400, now_epoch=now,
        max_repeat_weeks=4,  # 4-week window; shown 1 week ago → suppressed
    )
    assert result.updates == []


def test_v_prefix_same_version_not_selected_as_update(tmp_path: Path) -> None:
    """Installed '0.7.1' vs upstream 'v0.7.1' is the same version — must NOT
    appear as an update even when last_seen is old enough to pass the throttle."""
    engine = _engine(tmp_path)
    one_week = 7 * 86400
    now = 100_000_000
    with session_scope(engine) as s:
        _seed_library(
            s, thread_id=1,
            installed_version="0.7.1",
            latest_upstream_version="v0.7.1",
            upstream_last_updated_at=now - 10 * one_week,  # not a fresh update
            last_seen_in_digest_at=now - 5 * one_week,    # past the repeat window
        )
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 86400, now_epoch=now,
        max_repeat_weeks=4,
    )
    assert result.updates == []


def test_genuine_version_bump_still_selected(tmp_path: Path) -> None:
    """Installed '0.20.1' vs upstream 'v0.20.2 Extra' is a real update — MUST
    appear."""
    engine = _engine(tmp_path)
    one_week = 7 * 86400
    now = 100_000_000
    with session_scope(engine) as s:
        _seed_library(
            s, thread_id=1,
            installed_version="0.20.1",
            latest_upstream_version="v0.20.2 Extra",
            upstream_last_updated_at=now - 10 * one_week,  # not a fresh update
            last_seen_in_digest_at=now - 5 * one_week,    # past the repeat window
        )
    result = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 86400, now_epoch=now,
        max_repeat_weeks=4,
    )
    ids = {u.f95_thread_id for u in result.updates}
    assert ids == {1}


def test_force_surfaces_all_behind_ignoring_throttle(tmp_path: Path) -> None:
    """--force should surface every game where played != upstream, even ones
    shown minutes ago (which the normal throttle would suppress)."""
    engine = _engine(tmp_path)
    now = 100_000_000
    with session_scope(engine) as s:
        # Behind, shown 1 minute ago — normally suppressed.
        _seed_library(
            s, thread_id=1, installed_version="0.5.0",
            latest_upstream_version="0.7.0", upstream_last_updated_at=now - 999_999,
            last_seen_in_digest_at=now - 60,
        )
        # Already current — should NOT surface even with force.
        _seed_library(
            s, thread_id=2, installed_version="0.7.0",
            latest_upstream_version="0.7.0", upstream_last_updated_at=now - 999_999,
            last_seen_in_digest_at=now - 60,
        )
    normal = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 30, now_epoch=now,
        max_repeat_weeks=4,
    )
    assert normal.updates == []  # throttled

    forced = select_digest_candidates(
        engine=engine, previous_digest_run_at=now - 30, now_epoch=now,
        max_repeat_weeks=4, force_all_behind=True,
    )
    ids = {u.f95_thread_id for u in forced.updates}
    assert ids == {1}  # behind game surfaces; current game does not


def test_daily_fires_on_new_version_user_behind(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=42, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.5.0",
                      last_daily_notified_status="1")
    result = select_daily_candidates(
        engine=engine, f95_rows=[_f95(42, version="0.7.0")], now_epoch=500,
    )
    assert {u.f95_thread_id for u in result.updates} == {42}


def test_daily_silent_when_already_notified_for_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=42, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.7.0",
                      last_daily_notified_status="1")
    result = select_daily_candidates(
        engine=engine, f95_rows=[_f95(42, version="0.7.0")], now_epoch=500,
    )
    assert result.updates == []


def test_daily_fires_on_status_change_even_if_current(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=42, installed_version="0.7.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.7.0",
                      last_daily_notified_status="1")  # was ongoing
    result = select_daily_candidates(
        engine=engine, f95_rows=[_f95(42, version="0.7.0", status="2")],  # now completed
        now_epoch=500,
    )
    updates = result.updates
    assert {u.f95_thread_id for u in updates} == {42}
    assert updates[0].status == "2"


def test_daily_respects_hidden_and_ack(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with session_scope(engine) as s:
        _seed_library(s, thread_id=1, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.5.0", hidden=1)
        _seed_library(s, thread_id=2, installed_version="0.5.0",
                      latest_upstream_version="0.7.0",
                      last_daily_notified_version="0.5.0",
                      acknowledged_version="0.7.0")
    result = select_daily_candidates(
        engine=engine,
        f95_rows=[_f95(1, version="0.7.0"), _f95(2, version="0.7.0")],
        now_epoch=500,
    )
    assert result.updates == []
