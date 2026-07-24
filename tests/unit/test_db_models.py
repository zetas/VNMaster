import time
from pathlib import Path

from sqlalchemy import select

from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import (
    Base,
    ChangelogExtraction,
    DigestEntry,
    DigestRun,
    GameInstall,
    LibraryGame,
    Pairing,
)


def _make_engine(tmp_path: Path):
    db_path = tmp_path / "test.db"
    engine = create_engine_for(db_path)
    Base.metadata.create_all(engine)
    return engine


def test_library_game_round_trip(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    now = int(time.time())
    with session_scope(engine) as s:
        s.add(
            LibraryGame(
                f95_thread_id=42,
                game_title="Eternum",
                save_dir_name="Eternum-1234567890",
                last_played_at=now,
                latest_upstream_version="0.7.0",
                created_at=now,
                updated_at=now,
            )
        )
    with session_scope(engine) as s:
        found = s.execute(
            select(LibraryGame).where(LibraryGame.f95_thread_id == 42)
        ).scalar_one()
        assert found.game_title == "Eternum"
        assert found.hidden == 0


def test_pairing_unique(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    now = int(time.time())
    with session_scope(engine) as s:
        s.add(Pairing(f95_thread_id=1, save_dir_name="A", confidence=1.0, paired_at=now))
    with session_scope(engine) as s:
        found = s.execute(select(Pairing)).scalar_one()
        assert found.f95_thread_id == 1


def test_game_install_round_trip(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    now = int(time.time())
    with session_scope(engine) as session:
        session.add(
            GameInstall(
                f95_thread_id=42,
                game_title="A Game",
                version="v1",
                install_path="/tmp/A Game/v1",
                thread_url="https://f95zone.to/threads/42",
                platform="mac",
                host="MEGA",
                source_locator="masked",
                artifacts_json="[]",
                archive_hashes_json="{}",
                verification_json="[]",
                installed_at=now,
                updated_at=now,
            )
        )
    with session_scope(engine) as session:
        found = session.execute(select(GameInstall)).scalar_one()
        assert found.game_title == "A Game"
        assert found.platform == "mac"


def test_changelog_extraction_dedup_by_hash(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    now = int(time.time())
    with session_scope(engine) as s:
        s.add(
            ChangelogExtraction(
                f95_thread_id=10,
                content_hash="abc123",
                extraction_method="llm",
                versions_json="[]",
                extracted_at=now,
            )
        )
    with session_scope(engine) as s:
        rows = s.execute(select(ChangelogExtraction)).scalars().all()
        assert len(rows) == 1


def test_digest_run_and_entries_link(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    now = int(time.time())
    with session_scope(engine) as s:
        run = DigestRun(run_at=now, updates_count=0, llm_calls=0, llm_cost_usd=0.0)
        s.add(run)
        s.flush()
        s.add(
            DigestEntry(
                run_id=run.id,
                discord_message_id="m1",
                embed_index=0,
                kind="update",
                f95_thread_id=42,
            )
        )
    with session_scope(engine) as s:
        e = s.execute(select(DigestEntry)).scalar_one()
        assert e.kind == "update"
