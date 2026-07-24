import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_head_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    env = {**os.environ, "VNMASTER_DB_URL": f"sqlite:///{db}"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "src/vnmaster/db/migrations/alembic.ini",
            "upgrade",
            "head",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    engine = create_engine(f"sqlite:///{db}")
    names = set(inspect(engine).get_table_names())
    assert {
        "library_games",
        "pairings",
        "changelog_extractions",
        "digest_runs",
        "digest_entries",
    } <= names
    lib_cols = {c["name"] for c in inspect(engine).get_columns("library_games")}
    assert {"last_daily_notified_version", "last_daily_notified_status"} <= lib_cols
    run_cols = {c["name"] for c in inspect(engine).get_columns("digest_runs")}
    assert "kind" in run_cols
