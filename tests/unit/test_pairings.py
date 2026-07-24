"""TDD tests for cmd_pairings_list and cmd_unpair slash handlers."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from vnmaster.bot.slash import (
    NoSuchPairingError,
    cmd_pairings_list,
    cmd_unpair,
)
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, LibraryGame, Pairing


def _engine(tmp_path: Path):
    e = create_engine_for(tmp_path / "v.db")
    Base.metadata.create_all(e)
    return e


def _add_pairing(
    engine,
    *,
    f95_thread_id: int,
    save_dir_name: str | None = None,
    folder_name: str | None = None,
    confidence: float = 0.9,
    paired_at: int = 100,
) -> None:
    with session_scope(engine) as s:
        s.merge(Pairing(
            f95_thread_id=f95_thread_id,
            save_dir_name=save_dir_name,
            folder_name=folder_name,
            confidence=confidence,
            paired_at=paired_at,
        ))


def _add_library_game(engine, *, f95_thread_id: int, game_title: str) -> None:
    with session_scope(engine) as s:
        s.merge(LibraryGame(
            f95_thread_id=f95_thread_id,
            game_title=game_title,
            hidden=0,
            interested=0,
            created_at=100,
            updated_at=100,
        ))


# ---------------------------------------------------------------------------
# cmd_pairings_list
# ---------------------------------------------------------------------------

def test_pairings_list_empty_returns_friendly_message(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    result = cmd_pairings_list(engine=engine)
    assert "no pairings" in result.lower()


def test_pairings_list_shows_both_pairings(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=111, save_dir_name="GameSave-111")
    _add_pairing(engine, f95_thread_id=222, save_dir_name="GameSave-222")
    result = cmd_pairings_list(engine=engine)
    assert "111" in result
    assert "222" in result
    assert "GameSave-111" in result
    assert "GameSave-222" in result


def test_pairings_list_resolves_title_from_library_game(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=333, save_dir_name="MySave")
    _add_library_game(engine, f95_thread_id=333, game_title="Awesome Game")
    result = cmd_pairings_list(engine=engine)
    assert "Awesome Game" in result


def test_pairings_list_shows_unknown_when_no_library_game(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=444, save_dir_name="OrphanSave")
    result = cmd_pairings_list(engine=engine)
    assert "(unknown)" in result


def test_pairings_list_includes_count(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=100, save_dir_name="Save-A")
    _add_pairing(engine, f95_thread_id=200, save_dir_name="Save-B")
    result = cmd_pairings_list(engine=engine)
    # Must mention "2" somewhere (count)
    assert "2" in result


def test_pairings_list_ordered_by_thread_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=999, save_dir_name="Late")
    _add_pairing(engine, f95_thread_id=1, save_dir_name="Early")
    result = cmd_pairings_list(engine=engine)
    # Thread 1 should appear before thread 999 in output
    assert result.index("1") < result.index("999")


# ---------------------------------------------------------------------------
# cmd_unpair
# ---------------------------------------------------------------------------

def test_unpair_by_save_dir_name(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=555, save_dir_name="RemoveMe")
    result = cmd_unpair(engine=engine, name="RemoveMe")
    assert "555" in result
    with session_scope(engine) as s:
        row = s.execute(select(Pairing).where(Pairing.f95_thread_id == 555)).scalar_one_or_none()
    assert row is None


def test_unpair_by_folder_name(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=666, folder_name="MyFolder")
    result = cmd_unpair(engine=engine, name="MyFolder")
    assert "666" in result
    with session_scope(engine) as s:
        row = s.execute(select(Pairing).where(Pairing.f95_thread_id == 666)).scalar_one_or_none()
    assert row is None


def test_unpair_by_numeric_thread_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=777, save_dir_name="AnyName")
    result = cmd_unpair(engine=engine, name="777")
    assert "777" in result
    with session_scope(engine) as s:
        row = s.execute(select(Pairing).where(Pairing.f95_thread_id == 777)).scalar_one_or_none()
    assert row is None


def test_unpair_nonexistent_raises_no_such_pairing_error(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(NoSuchPairingError):
        cmd_unpair(engine=engine, name="ghost")


def test_unpair_returns_message_with_name(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _add_pairing(engine, f95_thread_id=888, save_dir_name="TargetSave")
    result = cmd_unpair(engine=engine, name="TargetSave")
    assert "TargetSave" in result


def test_unpair_multiple_matches_removes_all(tmp_path: Path) -> None:
    """save_dir_name match on two different thread IDs (unusual but possible)."""
    engine = _engine(tmp_path)
    # Direct DB insert to create two rows with the same save_dir_name
    # (f95_thread_id is PK so they have different PKs)
    _add_pairing(engine, f95_thread_id=1001, save_dir_name="SharedName")
    _add_pairing(engine, f95_thread_id=1002, folder_name="SharedName")
    result = cmd_unpair(engine=engine, name="SharedName")
    # Both rows removed
    with session_scope(engine) as s:
        rows = list(s.execute(select(Pairing)).scalars())
    assert len(rows) == 0
    # Result should indicate multiple removals
    assert "2" in result
