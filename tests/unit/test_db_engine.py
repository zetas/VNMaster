"""Regression tests for engine factories.

Specifically: create_readonly_engine must actually reject writes. Previous
implementations passed `?mode=ro` without `uri=True`, which SQLite silently
ignored — the engine appeared to work but allowed writes to F95Checker's DB.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy import text

from vnmaster.db.engine import create_engine_for, create_readonly_engine


def _seed_writable_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (i INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def test_readonly_engine_rejects_writes(tmp_path: Path) -> None:
    db = tmp_path / "ro.db"
    _seed_writable_db(db)
    engine = create_readonly_engine(db)
    with engine.connect() as c:
        with pytest.raises(OperationalError):
            c.execute(text("INSERT INTO t VALUES (2)"))
            c.commit()


def test_readonly_engine_allows_reads(tmp_path: Path) -> None:
    db = tmp_path / "ro.db"
    _seed_writable_db(db)
    engine = create_readonly_engine(db)
    with engine.connect() as c:
        rows = list(c.execute(text("SELECT i FROM t ORDER BY i")))
    assert rows == [(1,)]


def test_writable_engine_allows_writes(tmp_path: Path) -> None:
    db = tmp_path / "rw.db"
    engine = create_engine_for(db)
    with engine.connect() as c:
        c.execute(text("CREATE TABLE t (i INTEGER)"))
        c.execute(text("INSERT INTO t VALUES (1)"))
        c.commit()
        rows = list(c.execute(text("SELECT i FROM t")))
    assert rows == [(1,)]


def test_ensure_schema_creates_all_tables(tmp_path: Path) -> None:
    """Regression: vnmaster digest crashed with 'no such table: pairings'
    because the wizard never ran alembic against a fresh DB. ensure_schema
    is the idempotent fallback that materializes every model."""
    from sqlalchemy import inspect

    from vnmaster.db.engine import create_engine_for, ensure_schema

    db = tmp_path / "fresh.db"
    engine = create_engine_for(db)
    # Before ensure_schema — no tables.
    assert inspect(engine).get_table_names() == []

    ensure_schema(engine)
    names = set(inspect(engine).get_table_names())
    assert {
        "library_games", "pairings", "changelog_extractions",
        "digest_runs", "digest_entries", "game_installs",
    } <= names


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    """Calling ensure_schema twice must not raise or alter the DB."""
    from vnmaster.db.engine import create_engine_for, ensure_schema

    db = tmp_path / "fresh.db"
    engine = create_engine_for(db)
    ensure_schema(engine)
    ensure_schema(engine)  # must not raise


def test_ensure_schema_adds_daily_columns_and_backfills(tmp_path) -> None:
    from sqlalchemy import text
    from vnmaster.db.engine import create_engine_for, ensure_schema

    db = tmp_path / "old.db"
    engine = create_engine_for(db)
    # Simulate an old DB: library_games without the daily columns, one row.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE library_games ("
            "f95_thread_id INTEGER PRIMARY KEY, game_title VARCHAR NOT NULL, "
            "latest_upstream_version VARCHAR, status VARCHAR, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO library_games "
            "(f95_thread_id, game_title, latest_upstream_version, status, created_at, updated_at) "
            "VALUES (7, 'Eternum', '0.7.0', '2', 1, 1)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE digest_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, run_at INTEGER NOT NULL, "
            "updates_count INTEGER NOT NULL, llm_calls INTEGER NOT NULL, "
            "llm_cost_usd FLOAT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO digest_runs (run_at, updates_count, llm_calls, llm_cost_usd) "
            "VALUES (100, 0, 0, 0.0)"
        )

    ensure_schema(engine)

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT last_daily_notified_version, last_daily_notified_status "
            "FROM library_games WHERE f95_thread_id = 7"
        )).one()
        assert row[0] == "0.7.0"   # backfilled from latest_upstream_version
        assert row[1] == "2"        # backfilled from status
        kind = conn.execute(text("SELECT kind FROM digest_runs WHERE run_at = 100")).scalar_one()
        assert kind == "weekly"     # backfilled default
