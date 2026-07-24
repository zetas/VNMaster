from pathlib import Path

import pytest
from sqlalchemy import select

from vnmaster.bot.slash import (
    InvalidUrlError,
    cmd_pair, cmd_status,
)
from vnmaster.config import Config
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, Pairing


def _engine(tmp_path: Path):
    e = create_engine_for(tmp_path / "v.db")
    Base.metadata.create_all(e)
    return e


def _cfg(tmp_path: Path) -> Config:
    return Config.load(
        Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml"
    )


def test_pair_writes_to_pairings(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    cmd_pair(
        engine=engine,
        name="SecretSave-9999",
        f95_url="https://f95zone.to/threads/eternum.12345/",
        now_epoch=100,
    )
    with session_scope(engine) as s:
        row = s.execute(select(Pairing)).scalar_one()
        assert row.f95_thread_id == 12345
        assert row.save_dir_name == "SecretSave-9999"


def test_pair_invalid_url_raises(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(InvalidUrlError):
        cmd_pair(engine=engine, name="X", f95_url="https://example.com/", now_epoch=100)


def test_status_reports_counts(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    out = cmd_status(engine=engine)
    assert "last digest run" in out.lower() or "no digest runs yet" in out.lower()
