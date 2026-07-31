from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vnmaster.db.engine import create_engine_for, ensure_schema
from vnmaster.downloads.models import PlannedArtifact, ResolvedDownload
from vnmaster.downloads.service import ArtifactExecution, DownloadExecutionResult
from vnmaster.downloads.state import (
    AmbiguousInstallStateError,
    InstallStateNotFoundError,
    list_install_states,
    mark_rebuilt,
    resolve_install_state,
    save_install_state,
)


def _result(root: Path, *, version: str = "v1") -> DownloadExecutionResult:
    final_dir = root / "A Game" / version
    archive = final_dir / "archive" / "game.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"archive")
    game_dir = final_dir / "game" / "Example-pc" / "game"
    game_dir.mkdir(parents=True)
    (game_dir / "script.rpyc").write_bytes(b"renpy")
    artifact = PlannedArtifact(
        kind="game",
        title="A Game",
        version=version,
        thread_id=42,
        thread_url="https://f95zone.to/threads/42",
        group_name="Win/Linux",
        platform="windows",
        host="GOFILE",
        locator="https://gofile.io/d/example",
    )
    execution = ArtifactExecution(
        artifact=artifact,
        download=ResolvedDownload(
            "GOFILE",
            artifact.locator,
            "https://gofile.io/d/example",
            platform="windows",
            group_name="Win/Linux",
        ),
        output_path=Path("game"),
        archive_paths=(Path("archive/game.zip"),),
    )
    return DownloadExecutionResult(
        final_dir=final_dir,
        artifacts=(execution,),
        verification_checks=("game present",),
        renpy_game_dir=Path("game/Example-pc/game"),
        urm_path=None,
    )


def test_save_and_resolve_install_state(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "vnmaster.db")
    ensure_schema(engine)
    result = _result(tmp_path / "Games")
    messages: list[str] = []

    saved = save_install_state(engine, result, reporter=messages.append)

    assert saved.f95_thread_id == 42
    assert saved.platform == "windows"
    assert saved.host == "GOFILE"
    assert saved.archive_hashes["archive/game.zip"] == hashlib.sha256(b"archive").hexdigest()
    assert saved.artifacts[0]["source_locator"] == "https://gofile.io/d/example"
    assert resolve_install_state(engine, "A Game").id == saved.id
    assert resolve_install_state(engine, "42").id == saved.id
    assert resolve_install_state(engine, str(result.final_dir)).id == saved.id
    assert messages == ["Hashing preserved payload: archive/game.zip"]


def test_resolve_install_state_reports_missing_and_ambiguous(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "vnmaster.db")
    ensure_schema(engine)
    save_install_state(engine, _result(tmp_path / "Games", version="v1"))
    save_install_state(engine, _result(tmp_path / "Games", version="v2"))

    with pytest.raises(AmbiguousInstallStateError, match="Multiple recorded"):
        resolve_install_state(engine, "A Game")
    with pytest.raises(InstallStateNotFoundError, match="No recorded"):
        resolve_install_state(engine, "Missing")


def test_mark_rebuilt_updates_state(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "vnmaster.db")
    ensure_schema(engine)
    saved = save_install_state(engine, _result(tmp_path / "Games"))

    rebuilt = mark_rebuilt(engine, saved, verification_checks=("rebuilt",))

    assert rebuilt.last_rebuilt_at is not None
    assert rebuilt.verification_checks == ("rebuilt",)
    assert list_install_states(engine) == (rebuilt,)


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine_for(tmp_path / "vnmaster.db")
    ensure_schema(eng)
    return eng


def _fake_result(
    final_dir: Path, *, part: str | None = None, version: str = "v1"
) -> DownloadExecutionResult:
    archive = final_dir / "archive" / "game.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")
    artifact = PlannedArtifact(
        kind="game",
        title="Split Game",
        version=version,
        thread_id=42,
        thread_url="https://f95zone.to/threads/42",
        group_name="Win/Linux",
        platform="windows",
        host="GOFILE",
        locator="https://gofile.io/d/example",
        part=part,
    )
    execution = ArtifactExecution(
        artifact=artifact,
        download=ResolvedDownload(
            "GOFILE",
            artifact.locator,
            "https://gofile.io/d/example",
            platform="windows",
            group_name="Win/Linux",
        ),
        output_path=Path("game"),
        archive_paths=(Path("archive/game.zip"),),
    )
    return DownloadExecutionResult(
        final_dir=final_dir,
        artifacts=(execution,),
        verification_checks=("game present",),
        renpy_game_dir=None,
        urm_path=None,
    )


def test_part_save_prefixes_paths_and_records_part(engine, tmp_path: Path) -> None:
    root = tmp_path / "Split Game" / "v2.0"
    result = _fake_result(final_dir=root / "Part 1", part="Part 1")

    state = save_install_state(engine, result, part="Part 1", install_root=root)

    assert state.install_path == root
    entry = state.artifacts[0]
    assert entry["part"] == "Part 1"
    assert entry["archive_paths"][0].startswith("Part 1/")
    assert all(key.startswith("Part 1/") for key in state.archive_hashes)
    assert all(line.startswith("Part 1: ") for line in state.verification_checks)


def test_part_save_merges_and_keeps_sibling_parts(engine, tmp_path: Path) -> None:
    root = tmp_path / "Split Game" / "v2.0"
    save_install_state(
        engine, _fake_result(final_dir=root / "Part 1", part="Part 1"),
        part="Part 1", install_root=root,
    )
    save_install_state(
        engine, _fake_result(final_dir=root / "Part 2", part="Part 2"),
        part="Part 2", install_root=root,
    )

    state = save_install_state(
        engine, _fake_result(final_dir=root / "Part 1", part="Part 1"),
        part="Part 1", install_root=root,
    )

    parts = sorted({entry.get("part") for entry in state.artifacts})
    assert parts == ["Part 1", "Part 2"]
    assert any(key.startswith("Part 2/") for key in state.archive_hashes)


def test_legacy_save_is_unchanged(engine, tmp_path: Path) -> None:
    result = _fake_result(final_dir=tmp_path / "G" / "v1")

    state = save_install_state(engine, result)

    assert "part" not in state.artifacts[0] or state.artifacts[0]["part"] is None
    assert not any(key.startswith("/") for key in state.archive_hashes)
