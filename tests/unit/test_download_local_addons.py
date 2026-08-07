from __future__ import annotations

from pathlib import Path

import pytest

from vnmaster.db.engine import create_engine_for, ensure_schema
from vnmaster.downloads.local_addons import (
    LocalAddonError,
    discover_local_addon_targets,
    install_local_addon,
    prepare_local_addon,
    preview_local_addon,
)
from vnmaster.downloads.models import PlannedArtifact, ResolvedDownload
from vnmaster.downloads.service import ArtifactExecution, DownloadExecutionResult
from vnmaster.downloads.state import list_install_states, save_install_state


def _record_part(engine, root: Path, label: str, app_name: str) -> Path:
    part_root = root / label
    archive = part_root / "archive" / "game.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(f"{label} archive".encode())
    game_dir = (
        part_root
        / "game"
        / app_name
        / "Contents"
        / "Resources"
        / "autorun"
        / "game"
    )
    game_dir.mkdir(parents=True)
    (game_dir / "script.rpyc").write_bytes(b"renpy")
    artifact = PlannedArtifact(
        kind="game",
        title="Split Game",
        version="v1",
        thread_id=42,
        thread_url="https://f95zone.to/threads/42",
        group_name=label,
        platform="mac",
        host="GOFILE",
        locator="https://gofile.io/d/example",
        part=label,
    )
    execution = ArtifactExecution(
        artifact=artifact,
        download=ResolvedDownload(
            "GOFILE",
            artifact.locator,
            "https://gofile.io/d/example",
            platform="mac",
            group_name=label,
        ),
        output_path=Path("game"),
        archive_paths=(Path("archive/game.zip"),),
    )
    result = DownloadExecutionResult(
        final_dir=part_root,
        artifacts=(execution,),
        verification_checks=("game present",),
        renpy_game_dir=game_dir.relative_to(part_root),
        urm_path=None,
    )
    save_install_state(engine, result, part=label, install_root=root)
    return game_dir


@pytest.fixture
def recorded_game(tmp_path: Path):
    engine = create_engine_for(tmp_path / "vnmaster.db")
    ensure_schema(engine)
    root = tmp_path / "Games" / "Split Game" / "v1"
    first = _record_part(engine, root, "Part 1", "First.app")
    second = _record_part(engine, root, "Part 2", "Second.app")
    return engine, root, first, second


def test_discovers_each_multipart_app_as_a_target(recorded_game) -> None:
    engine, _root, _first, _second = recorded_game

    inventory = discover_local_addon_targets(engine)

    assert len(inventory.targets) == 2
    assert [target.part for target in inventory.targets] == ["Part 1", "Part 2"]
    assert "First.app" in inventory.targets[0].label
    assert "Second.app" in inventory.targets[1].label
    assert inventory.warnings == ()


def test_installs_preserves_backs_up_and_records_local_file(
    recorded_game,
    tmp_path: Path,
) -> None:
    engine, root, first, second = recorded_game
    (first / "incest_patch.rpa").write_bytes(b"old")
    source = tmp_path / "incest_patch.rpa"
    source.write_bytes(b"new")
    targets = discover_local_addon_targets(engine).targets

    with prepare_local_addon(source, name="Incest Patch") as prepared:
        plan = preview_local_addon(prepared, targets)
        assert plan.files_to_install == 2
        assert plan.files_to_overwrite == 1
        result = install_local_addon(engine, plan)

    assert (first / source.name).read_bytes() == b"new"
    assert (second / source.name).read_bytes() == b"new"
    assert len(result.targets) == 2
    first_backup = result.targets[0].backup_path / "files" / source.name
    assert first_backup.read_bytes() == b"old"
    assert result.targets[0].archive_path.read_bytes() == b"new"

    state = list_install_states(engine)[0]
    local_artifacts = [entry for entry in state.artifacts if entry.get("local")]
    assert len(local_artifacts) == 2
    assert {entry["part"] for entry in local_artifacts} == {"Part 1", "Part 2"}
    assert all(entry["renpy_target"].endswith("autorun/game") for entry in local_artifacts)
    assert any(key.startswith("Part 1/archive/local-addons/") for key in state.archive_hashes)
    assert any(key.startswith("Part 2/archive/local-addons/") for key in state.archive_hashes)
    assert not (root / "archive" / "local-addons").exists()


def test_failure_rolls_back_prior_targets_and_does_not_record(
    recorded_game,
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine, root, first, second = recorded_game
    (first / "patch.rpa").write_bytes(b"first-old")
    (second / "patch.rpa").write_bytes(b"second-old")
    source = tmp_path / "patch.rpa"
    source.write_bytes(b"new")
    targets = discover_local_addon_targets(engine).targets

    from vnmaster.downloads import local_addons as module

    real_apply = module.apply_addon_preview
    calls = 0

    def fail_second(preview):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated failure")
        return real_apply(preview)

    monkeypatch.setattr(module, "apply_addon_preview", fail_second)
    with prepare_local_addon(source) as prepared:
        plan = preview_local_addon(prepared, targets)
        with pytest.raises(LocalAddonError, match="rolled back"):
            install_local_addon(engine, plan)

    assert (first / "patch.rpa").read_bytes() == b"first-old"
    assert (second / "patch.rpa").read_bytes() == b"second-old"
    state = list_install_states(engine)[0]
    assert not any(entry.get("local") for entry in state.artifacts)
    assert not list(root.glob("Part */archive/local-addons/*"))
    assert not list(root.glob("Part */backups/local-addons/*"))


def test_rejects_symlink_source(tmp_path: Path) -> None:
    source = tmp_path / "patch.rpa"
    source.write_bytes(b"patch")
    link = tmp_path / "link.rpa"
    link.symlink_to(source)

    with pytest.raises(LocalAddonError, match="symbolic link"):
        with prepare_local_addon(link):
            pass


def test_rejects_symlink_inside_source_folder(tmp_path: Path) -> None:
    source = tmp_path / "mod"
    source.mkdir()
    payload = tmp_path / "outside.rpa"
    payload.write_bytes(b"outside")
    (source / "patch.rpa").symlink_to(payload)

    with pytest.raises(LocalAddonError, match="contains a symbolic link"):
        with prepare_local_addon(source):
            pass
