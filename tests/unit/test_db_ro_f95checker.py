import sqlite3
from pathlib import Path

import pytest

from vnmaster.db.ro_f95checker import F95CheckerDB, SchemaMismatchError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "f95checker" / "db_11x.sqlite3"


def test_open_and_schema_check_passes() -> None:
    db = F95CheckerDB.open(FIXTURE)
    db.check_schema()  # no raise


def test_iter_all_games_returns_rows() -> None:
    db = F95CheckerDB.open(FIXTURE)
    games = list(db.iter_all_games())
    assert len(games) == 2
    e = next(g for g in games if g.id == 42)
    assert e.name == "Eternum"
    assert e.version == "0.7.0"
    assert e.executable == "/Users/x/Games/Eternum/MyGame.app"
    assert e.tags == ["corruption", "slow-burn"]


def test_iter_games_updated_since_filter() -> None:
    db = F95CheckerDB.open(FIXTURE)
    fresh = list(db.iter_games_updated_since(0))
    assert len(fresh) == 2
    none = list(db.iter_games_updated_since(2 * 10**10))
    assert none == []


def test_schema_mismatch_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sqlite3"
    conn = sqlite3.connect(bad)
    # Missing required `version` and `last_updated` columns.
    conn.execute("CREATE TABLE games (id INTEGER, name TEXT)")
    conn.commit()
    conn.close()
    db = F95CheckerDB.open(bad)
    with pytest.raises(SchemaMismatchError):
        db.check_schema()


def test_minimal_schema_passes_and_yields_rows(tmp_path: Path) -> None:
    """A DB with only the CORE columns should work — optional columns become None.

    Regression: real F95Checker installs in the wild are missing `engine` and
    `executable` columns. VNMaster used to reject them; it must tolerate them.
    """
    minimal = tmp_path / "minimal.sqlite3"
    conn = sqlite3.connect(minimal)
    conn.execute(
        "CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT, "
        "version TEXT, last_updated INTEGER)"
    )
    conn.execute(
        "INSERT INTO games VALUES (1, 'Test Game', '0.1', 1700000000)"
    )
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(minimal)
    db.check_schema()  # must not raise
    games = list(db.iter_all_games())
    assert len(games) == 1
    g = games[0]
    assert g.id == 1
    assert g.name == "Test Game"
    assert g.version == "0.1"
    assert g.last_updated == 1700000000
    assert g.engine is None
    assert g.executable is None
    assert g.developer is None
    assert g.tags == []
    assert g.archived == 0


def test_last_successful_refresh_returns_epoch(tmp_path: Path) -> None:
    db_file = tmp_path / "with_settings.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT, "
        "version TEXT, last_updated INTEGER)"
    )
    conn.execute("CREATE TABLE settings (last_successful_refresh INTEGER)")
    conn.execute("INSERT INTO settings VALUES (1784097846)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() == 1784097846


def test_last_successful_refresh_zero_means_none(tmp_path: Path) -> None:
    """0 is F95Checker's never-refreshed default; treat as unknowable."""
    db_file = tmp_path / "zero.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (last_successful_refresh INTEGER)")
    conn.execute("INSERT INTO settings VALUES (0)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() is None


def test_last_successful_refresh_missing_column(tmp_path: Path) -> None:
    db_file = tmp_path / "no_col.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (bg_refresh_interval INTEGER)")
    conn.execute("INSERT INTO settings VALUES (30)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() is None


def test_last_successful_refresh_missing_table() -> None:
    """The checked-in fixture has a games table but no settings table."""
    db = F95CheckerDB.open(FIXTURE)
    assert db.last_successful_refresh_epoch() is None


def test_last_successful_refresh_empty_settings(tmp_path: Path) -> None:
    db_file = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (last_successful_refresh INTEGER)")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() is None


def test_last_successful_refresh_non_numeric_means_none(tmp_path: Path) -> None:
    db_file = tmp_path / "garbage.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (last_successful_refresh TEXT)")
    conn.execute("INSERT INTO settings VALUES ('not-a-number')")
    conn.commit()
    conn.close()

    db = F95CheckerDB.open(db_file)
    assert db.last_successful_refresh_epoch() is None
