"""Read-only accessor for F95Checker's local SQLite DB.

Never writes. Verifies a CORE schema on open and tolerates absent optional
columns by substituting NULL in the SELECT. This lets VNMaster work across
F95Checker versions that rename or omit non-essential columns.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Engine, bindparam, column, inspect, literal, select, table, text
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.engine import Row

from vnmaster.db.engine import create_readonly_engine

# Columns whose absence makes VNMaster's matching + digest infeasible.
CORE_COLUMNS = {"id", "name", "version", "last_updated"}

# Columns we'd prefer to read but can live without. If absent, the SELECT
# substitutes NULL and the F95CheckerGame field becomes None / empty.
OPTIONAL_COLUMNS = (
    "developer", "engine", "status", "changelog", "description",
    "image_url", "tags", "executable", "archived", "rating",
)

# Order of fields in the SELECT (and indices into the row tuple). Keep this
# stable — _row_to_game indexes by position.
SELECT_FIELDS = (
    "id", "name", "version", "developer", "engine", "status", "last_updated",
    "changelog", "description", "image_url", "tags", "executable",
    "archived", "rating",
)


class SchemaMismatchError(Exception):
    pass


@dataclass(frozen=True)
class F95CheckerGame:
    id: int
    name: str
    version: str | None
    developer: str | None
    engine: str | None
    status: str | None
    last_updated: int | None
    changelog: str | None
    description: str | None
    image_url: str | None
    tags: list[str]
    executable: str | None
    archived: int
    rating: int | None


class F95CheckerDB:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._present_columns: set[str] = set()

    @classmethod
    def open(cls, path: Path) -> "F95CheckerDB":
        return cls(create_readonly_engine(path))

    def check_schema(self) -> None:
        insp = inspect(self._engine)
        if "games" not in insp.get_table_names():
            raise SchemaMismatchError("F95Checker DB missing 'games' table")
        cols = {c["name"] for c in insp.get_columns("games")}
        missing_core = CORE_COLUMNS - cols
        if missing_core:
            raise SchemaMismatchError(
                f"F95Checker DB schema missing core columns: {sorted(missing_core)}"
            )
        self._present_columns = cols

    def _build_select_columns(self) -> list[ColumnElement[Any]]:
        """Build SELECT expressions that map to SELECT_FIELDS by position.
        Absent columns are substituted with NULL so _row_to_game can stay
        positional.
        """
        if not self._present_columns:
            # check_schema not called yet — populate lazily
            insp = inspect(self._engine)
            self._present_columns = {
                c["name"] for c in insp.get_columns("games")
            }
        expressions: list[ColumnElement[Any]] = []
        for field in SELECT_FIELDS:
            if field in self._present_columns:
                expressions.append(column(field))
            else:
                expressions.append(literal(None).label(field))
        return expressions

    def iter_all_games(self) -> Iterator[F95CheckerGame]:
        statement = select(*self._build_select_columns()).select_from(table("games"))
        with self._engine.connect() as conn:
            for row in conn.execute(statement):
                yield _row_to_game(row)

    def iter_games_updated_since(self, since_epoch: int) -> Iterator[F95CheckerGame]:
        statement = (
            select(*self._build_select_columns())
            .select_from(table("games"))
            .where(column("last_updated") > bindparam("since"))
        )
        with self._engine.connect() as conn:
            for row in conn.execute(statement, {"since": since_epoch}):
                yield _row_to_game(row)

    def last_successful_refresh_epoch(self) -> int | None:
        """Epoch of F95Checker's last completed full-refresh pass.

        Returns None when the settings table or column is absent (other
        F95Checker versions) or the value is 0 (never refreshed). None means
        "can't assess staleness", not "stale".
        """
        insp = inspect(self._engine)
        if "settings" not in insp.get_table_names():
            return None
        cols = {c["name"] for c in insp.get_columns("settings")}
        if "last_successful_refresh" not in cols:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT last_successful_refresh FROM settings")
            ).first()
        if row is None or not row[0]:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError, OverflowError):
            return None


def _row_to_game(row: Row[tuple[object, ...]]) -> F95CheckerGame:
    return F95CheckerGame(
        id=row[0],
        name=row[1],
        version=row[2],
        developer=row[3],
        engine=row[4],
        status=row[5],
        last_updated=row[6],
        changelog=row[7],
        description=row[8],
        image_url=row[9],
        tags=[t.strip() for t in (row[10] or "").split(",") if t.strip()],
        executable=row[11],
        archived=row[12] or 0,
        rating=row[13],
    )
