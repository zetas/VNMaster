"""SQLAlchemy engine + session factory for vnmaster.db."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_engine_for(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", future=True)


def ensure_schema(engine: Engine) -> None:
    """Create any vnmaster.db tables that don't exist yet.

    Idempotent and safe to call on every entrypoint. Used in place of
    running alembic migrations against a fresh DB: the wizard never
    invoked alembic, so vnmaster.db ended up an empty SQLite file and
    the first `vnmaster digest` crashed with 'no such table: pairings'.
    """
    # Local import to avoid a circular dep at module load time.
    from vnmaster.db.models import Base
    Base.metadata.create_all(engine)
    _ensure_extra_columns(engine)


def _ensure_extra_columns(engine: Engine) -> None:
    """Add columns introduced after a table was first created.

    `create_all` never ALTERs existing tables, so a model column added later
    won't appear on an already-created table. This idempotently adds such
    columns (defaulted/backfilled) so old DBs self-heal on the next run.
    """
    with engine.begin() as conn:
        lib_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(library_games)")
        }
        if "status_changed" not in lib_cols:
            conn.exec_driver_sql(
                "ALTER TABLE library_games ADD COLUMN status_changed INTEGER NOT NULL DEFAULT 0"
            )
        if "last_daily_notified_version" not in lib_cols:
            conn.exec_driver_sql(
                "ALTER TABLE library_games ADD COLUMN last_daily_notified_version VARCHAR"
            )
            conn.exec_driver_sql(
                "UPDATE library_games SET last_daily_notified_version = latest_upstream_version "
                "WHERE last_daily_notified_version IS NULL"
            )
        if "last_daily_notified_status" not in lib_cols:
            conn.exec_driver_sql(
                "ALTER TABLE library_games ADD COLUMN last_daily_notified_status VARCHAR"
            )
            conn.exec_driver_sql(
                "UPDATE library_games SET last_daily_notified_status = status "
                "WHERE last_daily_notified_status IS NULL"
            )
        run_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(digest_runs)")
        }
        if "kind" not in run_cols:
            conn.exec_driver_sql(
                "ALTER TABLE digest_runs ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'weekly'"
            )


def create_readonly_engine(db_path: Path) -> Engine:
    db_path = db_path.absolute()

    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    return create_engine("sqlite://", creator=_connect, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
