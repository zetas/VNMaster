"""Tests for the `vnmaster suggest-pairs` CLI command (Deliverable 3)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from vnmaster.cli import main
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, Pairing

CONFIG_FIXTURE = Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml"


def _make_f95_db(db_path: Path, games: list[tuple]) -> None:
    """Create a minimal F95Checker-shaped SQLite with the given game rows.

    Row tuple: (id, name, version, executable)
    Other columns are filled with stub values.
    """
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE games (
            id INTEGER PRIMARY KEY,
            name TEXT,
            version TEXT,
            developer TEXT,
            engine TEXT,
            status TEXT,
            last_updated INTEGER,
            changelog TEXT,
            description TEXT,
            image_url TEXT,
            tags TEXT,
            executable TEXT,
            archived INTEGER,
            rating INTEGER
        )
        """
    )
    for gid, name, version, exe in games:
        cur.execute(
            "INSERT INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, name, version, "Dev", "Ren'Py", "ongoing",
             now, "", "", "", "", exe, 0, None),
        )
    conn.commit()
    conn.close()


def _make_renpy_saves(saves_root: Path, saves: list[tuple[str, str | None]]) -> None:
    """Create minimal RenPy-style save dirs with valid ZIP-format .save files.

    Each entry is (dir_name, version_str_or_None). Ren'Py saves are ZIPs
    containing a `json` member with a `_version` key.
    """
    import io
    import json
    import zipfile

    for dir_name, version in saves:
        save_dir = saves_root / dir_name
        save_dir.mkdir(parents=True, exist_ok=True)
        # Write a valid ZIP-format .save file so scan_play_history picks it up.
        save_file = save_dir / "1-LT1.save"
        meta = {"_version": version or ""}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("json", json.dumps(meta))
        save_file.write_bytes(buf.getvalue())


def _make_config(tmp_path: Path, vnmaster_db: Path, f95_db: Path, saves_root: Path) -> Path:
    config_text = CONFIG_FIXTURE.read_text()
    config_text = config_text.replace(
        '"~/Library/Application Support/VNMaster/vnmaster.db"',
        f'"{vnmaster_db}"',
    )
    config_text = config_text.replace(
        '"~/Library/Application Support/F95Checker/db.sqlite3"',
        f'"{f95_db}"',
    )
    config_text = config_text.replace(
        '"~/Library/RenPy"',
        f'"{saves_root}"',
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text)
    return config_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_suggest_pairs_in_help(tmp_path: Path) -> None:
    """The suggest-pairs command must appear in vnmaster --help."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "suggest-pairs" in result.output


def test_suggest_pairs_help_text(tmp_path: Path) -> None:
    """The suggest-pairs --help should mention --apply."""
    runner = CliRunner()
    result = runner.invoke(main, ["suggest-pairs", "--help"])
    assert result.exit_code == 0
    assert "--apply" in result.output


def test_suggest_pairs_prints_table(tmp_path: Path, monkeypatch) -> None:
    """Without --apply: prints corroborated suggestion for a version-match save."""
    saves_root = tmp_path / "RenPy"
    f95_db = tmp_path / "f95.sqlite3"
    vnmaster_db = tmp_path / "v.db"

    engine = create_engine_for(vnmaster_db)
    Base.metadata.create_all(engine)

    # Desert Stalker: fuzzy ~89 (< 90 threshold) but version 0.20.3.1 matches.
    _make_f95_db(f95_db, [(101, "Desert Stalker", "0.20.3.1", None)])
    _make_renpy_saves(saves_root, [("DesertStalkerEA-100001", "0.20.3.1")])

    config_file = _make_config(tmp_path, vnmaster_db, f95_db, saves_root)
    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, ["suggest-pairs", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    # Should show the save dir name
    assert "DesertStalkerEA-100001" in result.output
    # Should show the suggestion label
    assert "corroborated" in result.output


def test_suggest_pairs_apply_writes_pairing(tmp_path: Path, monkeypatch) -> None:
    """With --apply: corroborated suggestion should be persisted to pairings table."""
    saves_root = tmp_path / "RenPy"
    f95_db = tmp_path / "f95.sqlite3"
    vnmaster_db = tmp_path / "v.db"

    engine = create_engine_for(vnmaster_db)
    Base.metadata.create_all(engine)

    _make_f95_db(f95_db, [(101, "Desert Stalker", "0.20.3.1", None)])
    _make_renpy_saves(saves_root, [("DesertStalkerEA-100001", "0.20.3.1")])

    config_file = _make_config(tmp_path, vnmaster_db, f95_db, saves_root)
    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, ["suggest-pairs", "--apply", "--config", str(config_file)])
    assert result.exit_code == 0, result.output

    # Verify the pairing was written.
    with session_scope(engine) as s:
        row = s.execute(
            select(Pairing).where(Pairing.f95_thread_id == 101)
        ).scalar_one_or_none()
    assert row is not None
    assert row.save_dir_name == "DesertStalkerEA-100001"
    assert row.confidence == pytest.approx(0.85)


def test_suggest_pairs_apply_skips_weak(tmp_path: Path, monkeypatch) -> None:
    """With --apply: a weak match (fuzzy < 70, no version) is NOT persisted."""
    saves_root = tmp_path / "RenPy"
    f95_db = tmp_path / "f95.sqlite3"
    vnmaster_db = tmp_path / "v.db"

    engine = create_engine_for(vnmaster_db)
    Base.metadata.create_all(engine)

    _make_f95_db(f95_db, [(101, "Desert Stalker", "0.20.3.1", None)])
    # Teste → fuzzy ~60 vs Desert Stalker, version 7.0 doesn't match 0.20.3.1
    _make_renpy_saves(saves_root, [("Teste-1764891901", "7.0")])

    config_file = _make_config(tmp_path, vnmaster_db, f95_db, saves_root)
    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, ["suggest-pairs", "--apply", "--config", str(config_file)])
    assert result.exit_code == 0, result.output

    with session_scope(engine) as s:
        rows = s.execute(select(Pairing)).scalars().all()
    assert len(rows) == 0


def test_suggest_pairs_apply_respects_no_clobber(tmp_path: Path, monkeypatch) -> None:
    """With --apply: an existing manual pairing (confidence=1.0) is not overwritten."""
    saves_root = tmp_path / "RenPy"
    f95_db = tmp_path / "f95.sqlite3"
    vnmaster_db = tmp_path / "v.db"

    engine = create_engine_for(vnmaster_db)
    Base.metadata.create_all(engine)

    now = int(time.time())
    with session_scope(engine) as s:
        s.add(Pairing(
            f95_thread_id=101,
            save_dir_name="ManuallyPaired",
            confidence=1.0,
            paired_at=now,
        ))

    _make_f95_db(f95_db, [(101, "Desert Stalker", "0.20.3.1", None)])
    _make_renpy_saves(saves_root, [("DesertStalkerEA-100001", "0.20.3.1")])

    config_file = _make_config(tmp_path, vnmaster_db, f95_db, saves_root)
    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, ["suggest-pairs", "--apply", "--config", str(config_file)])
    assert result.exit_code == 0, result.output

    with session_scope(engine) as s:
        row = s.execute(
            select(Pairing).where(Pairing.f95_thread_id == 101)
        ).scalar_one()
    # Manual pairing should survive untouched.
    assert row.save_dir_name == "ManuallyPaired"
    assert row.confidence == 1.0
