"""Tests for _persist_learned_pairings (Deliverable 2)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, Pairing
from vnmaster.matcher import LearnedPairing
from vnmaster.pipeline import _persist_learned_pairings


def _make_engine(tmp_path: Path):
    db_path = tmp_path / "test.db"
    engine = create_engine_for(db_path)
    Base.metadata.create_all(engine)
    return engine


def _lp(
    thread_id: int,
    save_dir: str | None = "MySave",
    folder: str | None = None,
    confidence: float = 0.95,
    method: str = "name",
) -> LearnedPairing:
    return LearnedPairing(
        f95_thread_id=thread_id,
        save_dir_name=save_dir,
        folder_name=folder,
        confidence=confidence,
        method=method,
    )


def test_persist_fresh_insert(tmp_path: Path) -> None:
    """A learned pairing with no existing row should be inserted."""
    engine = _make_engine(tmp_path)
    lp = _lp(42, save_dir="Eternum-1234567890", confidence=0.95)

    _persist_learned_pairings(engine, [lp], now_epoch=1700_000_000)

    with session_scope(engine) as s:
        row = s.execute(
            select(Pairing).where(Pairing.f95_thread_id == 42)
        ).scalar_one()
        assert row.save_dir_name == "Eternum-1234567890"
        assert row.confidence == 0.95
        assert row.paired_at == 1700_000_000


def test_persist_manual_not_clobbered(tmp_path: Path) -> None:
    """An existing manual pairing (confidence=1.0) must never be overwritten
    by an auto-learned pairing at 0.95."""
    engine = _make_engine(tmp_path)
    now = 1700_000_000
    with session_scope(engine) as s:
        s.add(Pairing(
            f95_thread_id=42,
            save_dir_name="ManualSave",
            confidence=1.0,
            paired_at=now,
        ))

    lp = _lp(42, save_dir="NewSave", confidence=0.95)
    _persist_learned_pairings(engine, [lp], now_epoch=now + 100)

    with session_scope(engine) as s:
        row = s.execute(
            select(Pairing).where(Pairing.f95_thread_id == 42)
        ).scalar_one()
        # Manual pairing should survive untouched
        assert row.save_dir_name == "ManualSave"
        assert row.confidence == 1.0
        assert row.paired_at == now


def test_persist_lower_does_not_overwrite_higher(tmp_path: Path) -> None:
    """A name-match pairing (0.95) should not be downgraded by a version-match
    pairing (0.85) when run again."""
    engine = _make_engine(tmp_path)
    now = 1700_000_000
    with session_scope(engine) as s:
        s.add(Pairing(
            f95_thread_id=42,
            save_dir_name="DesertStalker-1234",
            confidence=0.95,
            paired_at=now,
        ))

    lp_low = _lp(42, save_dir="DesertStalker-1234", confidence=0.85, method="version")
    _persist_learned_pairings(engine, [lp_low], now_epoch=now + 200)

    with session_scope(engine) as s:
        row = s.execute(
            select(Pairing).where(Pairing.f95_thread_id == 42)
        ).scalar_one()
        assert row.confidence == 0.95
        assert row.paired_at == now


def test_persist_higher_overwrites_lower(tmp_path: Path) -> None:
    """If we later get a name-match (0.95) for something that was previously
    saved as a version-match (0.85), it should upgrade the record."""
    engine = _make_engine(tmp_path)
    now = 1700_000_000
    with session_scope(engine) as s:
        s.add(Pairing(
            f95_thread_id=42,
            save_dir_name="DesertStalker-1234",
            confidence=0.85,
            paired_at=now,
        ))

    lp_high = _lp(42, save_dir="DesertStalker-1234", confidence=0.95, method="name")
    later = now + 300
    _persist_learned_pairings(engine, [lp_high], now_epoch=later)

    with session_scope(engine) as s:
        row = s.execute(
            select(Pairing).where(Pairing.f95_thread_id == 42)
        ).scalar_one()
        assert row.confidence == 0.95
        assert row.paired_at == later


def test_persist_multiple_learned_pairings(tmp_path: Path) -> None:
    """Multiple learned pairings are all inserted correctly."""
    engine = _make_engine(tmp_path)
    now = 1700_000_000
    lps = [
        _lp(1, save_dir="Save1", confidence=0.95),
        _lp(2, save_dir="Save2", confidence=0.85, method="version"),
    ]
    _persist_learned_pairings(engine, lps, now_epoch=now)

    with session_scope(engine) as s:
        rows = s.execute(select(Pairing).order_by(Pairing.f95_thread_id)).scalars().all()
        assert len(rows) == 2
        assert rows[0].f95_thread_id == 1
        assert rows[1].f95_thread_id == 2
