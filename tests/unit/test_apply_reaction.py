from pathlib import Path

from sqlalchemy import select

from vnmaster.bot.apply_reaction import ReactionAction, apply_reaction
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, LibraryGame


def _engine(tmp_path: Path):
    e = create_engine_for(tmp_path / "v.db")
    Base.metadata.create_all(e)
    return e


def _seed_library_game(engine, thread_id=42, **overrides) -> None:
    with session_scope(engine) as s:
        defaults = dict(
            f95_thread_id=thread_id, game_title="Eternum",
            latest_upstream_version="0.7.0", created_at=1, updated_at=1,
        )
        defaults.update(overrides)
        s.add(LibraryGame(**defaults))


def test_update_hide_sets_hidden_flag(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed_library_game(engine)
    apply_reaction(
        engine=engine, thread_id=42, kind="update",
        action=ReactionAction.HIDE, now_epoch=100,
    )
    with session_scope(engine) as s:
        g = s.execute(select(LibraryGame).where(LibraryGame.f95_thread_id == 42)).scalar_one()
        assert g.hidden == 1


def test_update_interested_sets_flag(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed_library_game(engine)
    apply_reaction(
        engine=engine, thread_id=42, kind="update",
        action=ReactionAction.INTERESTED, now_epoch=100,
    )
    with session_scope(engine) as s:
        g = s.execute(select(LibraryGame).where(LibraryGame.f95_thread_id == 42)).scalar_one()
        assert g.interested == 1


def test_update_acknowledged_records_current_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed_library_game(engine, latest_upstream_version="0.9.1")
    apply_reaction(
        engine=engine, thread_id=42, kind="update",
        action=ReactionAction.ACKNOWLEDGED, now_epoch=100,
    )
    with session_scope(engine) as s:
        g = s.execute(select(LibraryGame).where(LibraryGame.f95_thread_id == 42)).scalar_one()
        assert g.acknowledged_version == "0.9.1"


def test_unknown_kind_raises(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        apply_reaction(
            engine=engine, thread_id=1, kind="garbage",
            action=ReactionAction.HIDE, now_epoch=100,
        )
