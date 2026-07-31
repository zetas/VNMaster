from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from vnmaster.downloads.models import (
    DownloadMirror,
    DownloadPlan,
    PlannedArtifact,
    ResolvedDownload,
    ThreadInfo,
)
from vnmaster.downloads.service import (
    ArtifactDownloadError,
    DestinationExistsError,
    _execute_pairs,
    execute_download_plan,
    execute_download_plan_detailed,
    execute_multipart_plan,
)
from vnmaster.downloads.urm import URM_RPA_NAME


def _plan() -> DownloadPlan:
    game = ThreadInfo(
        thread_id=42,
        title="A Game",
        version="v1.2",
        thread_type=1,
        url="https://f95zone.to/threads/.42/",
        downloads=(),
    )
    artifact = PlannedArtifact(
        kind="game",
        title="A Game",
        version="v1.2",
        thread_id=42,
        thread_url=game.url,
        group_name="Mac",
        platform="mac",
        host="MEGA",
        locator="https://f95zone.to/masked/mega.nz/token",
    )
    return DownloadPlan(game=game, artifacts=(artifact,))


def test_execute_plan_publishes_atomically_without_manifest(tmp_path: Path) -> None:
    def downloader(url: str, destination: Path) -> list[Path]:
        destination.mkdir(parents=True)
        payload = destination / "game.zip"
        payload.write_bytes(b"archive")
        return [payload]

    def unpacker(downloaded: list[Path], destination: Path) -> None:
        game_dir = (
            destination
            / "A Game.app"
            / "Contents"
            / "Resources"
            / "autorun"
            / "game"
        )
        game_dir.mkdir(parents=True)
        (game_dir / "script.rpyc").write_bytes(b"renpy")

    mods_dir = tmp_path / "Mods"
    mods_dir.mkdir()
    with zipfile.ZipFile(mods_dir / "_0x52_URM.zip", "w") as bundle:
        bundle.writestr(URM_RPA_NAME, b"urm")

    result = execute_download_plan(
        _plan(),
        resolved_urls=["https://mega.nz/file/abc#secret-key"],
        destination_root=tmp_path,
        urm_mods_dir=mods_dir,
        downloader=downloader,
        unpacker=unpacker,
    )
    assert result == tmp_path / "A Game" / "v1.2"
    assert (result / "game" / "A Game.app").is_dir()
    assert (
        result
        / "game"
        / "A Game.app"
        / "Contents"
        / "Resources"
        / "autorun"
        / "game"
        / URM_RPA_NAME
    ).read_bytes() == b"urm"
    assert (result / "archive" / "game.zip").read_bytes() == b"archive"
    assert not (result / "manifest.json").exists()
    assert not list(tmp_path.glob(".vnmaster-fetch-*"))


def test_execute_plan_falls_back_to_next_mirror(tmp_path: Path) -> None:
    plan = _plan()
    artifact = plan.artifacts[0]
    plan = DownloadPlan(
        plan.game,
        (
            PlannedArtifact(
                **{
                    **artifact.__dict__,
                    "alternate_mirrors": (
                        DownloadMirror("GOFILE", "https://gofile.io/d/good"),
                    ),
                }
            ),
        ),
    )
    attempts: list[str] = []
    messages: list[str] = []

    def downloader(url: str, destination: Path) -> list[Path]:
        attempts.append(url)
        destination.mkdir(parents=True)
        partial = destination / "partial.zip"
        partial.write_bytes(b"partial")
        if "mega.nz" in url:
            raise RuntimeError("HTTP 403")
        payload = destination / "game.zip"
        payload.write_bytes(b"archive")
        return [payload]

    def unpacker(downloaded: list[Path], destination: Path) -> None:
        destination.mkdir(parents=True)
        (destination / "A Game.app").mkdir()

    result = execute_download_plan(
        plan,
        resolved_downloads=[
            (
                ResolvedDownload(
                    "MEGA", artifact.locator, "https://mega.nz/file/bad#key"
                ),
                ResolvedDownload(
                    "GOFILE",
                    "https://gofile.io/d/good",
                    "https://gofile.io/d/good",
                    platform="windows",
                    group_name="Win/Linux",
                ),
            )
        ],
        destination_root=tmp_path,
        downloader=downloader,
        unpacker=unpacker,
        reporter=messages.append,
    )

    assert attempts == ["https://mega.nz/file/bad#key", "https://gofile.io/d/good"]
    assert any("MEGA failed: HTTP 403" in message for message in messages)
    assert (result / "game" / "A Game.app").is_dir()
    assert (result / "archive" / "game.zip").read_bytes() == b"archive"
    assert not (result / "archive" / "partial.zip").exists()
    assert not (result / "manifest.json").exists()
    assert not (result / ".attempts").exists()


def test_execute_plan_falls_back_after_extraction_failure(tmp_path: Path) -> None:
    calls = 0

    def downloader(url: str, destination: Path) -> list[Path]:
        destination.mkdir(parents=True)
        payload = destination / "game.zip"
        payload.write_bytes(b"archive")
        return [payload]

    def unpacker(downloaded: list[Path], destination: Path) -> None:
        nonlocal calls
        calls += 1
        destination.mkdir(parents=True)
        (destination / "partial").write_text("partial")
        if calls == 1:
            raise RuntimeError("bad archive")

    result = execute_download_plan(
        _plan(),
        resolved_downloads=[
            (
                ResolvedDownload("ONE", "one", "https://one.example/game.zip"),
                ResolvedDownload("TWO", "two", "https://two.example/game.zip"),
            )
        ],
        destination_root=tmp_path,
        downloader=downloader,
        unpacker=unpacker,
    )
    assert calls == 2
    assert (result / "game" / "partial").read_text() == "partial"
    assert (result / "archive" / "game.zip").read_bytes() == b"archive"


def test_execute_plan_applies_selected_multifile_mod(tmp_path: Path) -> None:
    game_artifact = _plan().artifacts[0]
    addon_artifact = PlannedArtifact(
        kind="addon",
        title="A Game Multi-Mod",
        version="v1.2",
        thread_id=99,
        thread_url="https://f95zone.to/threads/99",
        group_name="Cheat and Walkthrough Mod",
        platform=None,
        host="MEGA",
        locator="https://mega.nz/file/mod",
    )
    plan = DownloadPlan(_plan().game, (game_artifact, addon_artifact))

    def downloader(url: str, destination: Path) -> list[Path]:
        destination.mkdir(parents=True)
        name = "mod.zip" if url.endswith("/mod") else "game.zip"
        payload = destination / name
        payload.write_bytes(b"archive")
        return [payload]

    def unpacker(downloaded: list[Path], destination: Path) -> None:
        if downloaded[0].name == "game.zip":
            game_dir = destination / "Example-pc" / "game"
            game_dir.mkdir(parents=True)
            (game_dir / "script.rpyc").write_bytes(b"renpy")
            existing = game_dir / "code" / "scene.rpyc"
            existing.parent.mkdir()
            existing.write_bytes(b"original")
            return
        packaged_game = destination / "Multi-Mod" / "game"
        replacement = packaged_game / "code" / "scene.rpyc"
        replacement.parent.mkdir(parents=True)
        replacement.write_bytes(b"modded")
        new_file = packaged_game / "gui" / "cheat.png"
        new_file.parent.mkdir()
        new_file.write_bytes(b"new")

    messages: list[str] = []
    execution = execute_download_plan_detailed(
        plan,
        resolved_urls=[
            "https://example.com/game",
            "https://example.com/mod",
        ],
        destination_root=tmp_path,
        downloader=downloader,
        unpacker=unpacker,
        reporter=messages.append,
    )

    installed_game = execution.final_dir / "game" / "Example-pc" / "game"
    assert (installed_game / "code" / "scene.rpyc").read_bytes() == b"modded"
    assert (installed_game / "gui" / "cheat.png").read_bytes() == b"new"
    assert (
        execution.final_dir
        / "archive"
        / "addons"
        / "02-A Game Multi-Mod"
        / "mod.zip"
    ).read_bytes() == b"archive"
    assert execution.artifacts[1].addon_merge is not None
    assert execution.artifacts[1].addon_merge.files_overwritten == 1
    assert "verified 1 installed add-on(s)" in execution.verification_checks
    assert any("Add-on install preview" in message for message in messages)
    assert any("2 files (1 overwritten)" in message for message in messages)


def test_execute_plan_reports_all_mirror_failures_and_cleans_staging(
    tmp_path: Path,
) -> None:
    def downloader(url: str, destination: Path) -> list[Path]:
        destination.mkdir(parents=True)
        raise RuntimeError(f"unavailable {url.rsplit('/', 1)[-1]}")

    with pytest.raises(ArtifactDownloadError, match="ONE: unavailable one") as exc_info:
        execute_download_plan(
            _plan(),
            resolved_downloads=[
                (
                    ResolvedDownload("ONE", "one", "https://example.com/one"),
                    ResolvedDownload("TWO", "two", "https://example.com/two"),
                )
            ],
            destination_root=tmp_path,
            downloader=downloader,
        )
    assert "TWO: unavailable two" in str(exc_info.value)
    assert not list(tmp_path.glob(".vnmaster-fetch-*"))


def test_execute_plan_refuses_to_overwrite_existing_version(tmp_path: Path) -> None:
    (tmp_path / "A Game" / "v1.2").mkdir(parents=True)
    with pytest.raises(DestinationExistsError, match="refusing to overwrite"):
        execute_download_plan(
            _plan(),
            resolved_urls=["https://mega.nz/file/abc#key"],
            destination_root=tmp_path,
        )


def test_execute_pairs_replaces_existing_dir_when_allowed(tmp_path: Path) -> None:
    artifact = _plan().artifacts[0]
    marker = {"run": 1}

    def downloader(url: str, destination: Path) -> list[Path]:
        destination.mkdir(parents=True)
        payload = destination / "game.zip"
        payload.write_bytes(b"archive")
        return [payload]

    def unpacker(downloaded: list[Path], destination: Path) -> None:
        destination.mkdir(parents=True)
        (destination / f"run-{marker['run']}.marker").write_text("marker")

    final_dir = tmp_path / "A Game" / "v1.2"
    pairs = [
        (
            artifact,
            (ResolvedDownload("MEGA", artifact.locator, "https://mega.nz/file/abc#key"),),
        )
    ]

    _execute_pairs(
        pairs,
        final_dir=final_dir,
        staging_parent=tmp_path,
        urm_mods_dir=None,
        downloader=downloader,
        unpacker=unpacker,
        reporter=lambda _message: None,
        replace_existing=True,
    )
    assert (final_dir / "game" / "run-1.marker").exists()

    marker["run"] = 2
    _execute_pairs(
        pairs,
        final_dir=final_dir,
        staging_parent=tmp_path,
        urm_mods_dir=None,
        downloader=downloader,
        unpacker=unpacker,
        reporter=lambda _message: None,
        replace_existing=True,
    )

    assert (final_dir / "game" / "run-2.marker").exists()
    assert not (final_dir / "game" / "run-1.marker").exists()
    assert list(tmp_path.glob(".vnmaster-previous-*")) == []


def _part_plan() -> DownloadPlan:
    game = ThreadInfo(
        thread_id=42,
        title="A Game",
        version="v1.2",
        thread_type=1,
        url="https://f95zone.to/threads/.42/",
        downloads=(),
    )
    part1 = PlannedArtifact(
        kind="game",
        title="A Game",
        version="v1.2",
        thread_id=42,
        thread_url=game.url,
        group_name="Mac",
        platform=None,
        host="MEGA",
        locator="https://f95zone.to/masked/mega.nz/part1",
        part="Part 1",
    )
    part2 = PlannedArtifact(
        kind="game",
        title="A Game",
        version="v1.2",
        thread_id=42,
        thread_url=game.url,
        group_name="Mac",
        platform=None,
        host="MEGA",
        locator="https://f95zone.to/masked/mega.nz/part2",
        part="Part 2",
    )
    addon = PlannedArtifact(
        kind="addon",
        title="Bonus Extras",
        version="v1.2",
        thread_id=99,
        thread_url="https://f95zone.to/threads/99",
        group_name="Extras",
        platform=None,
        host="MEGA",
        locator="https://f95zone.to/masked/mega.nz/patch",
    )
    return DownloadPlan(game=game, artifacts=(part1, part2, addon))


def _multipart_downloader(url: str, destination: Path) -> list[Path]:
    destination.mkdir(parents=True)
    payload = destination / "payload.zip"
    payload.write_bytes(url.encode())
    return [payload]


def _multipart_unpacker(downloaded: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True)
    (destination / "script.rpyc").write_bytes(downloaded[0].read_bytes())


def test_multipart_publishes_each_part_into_its_own_dir(tmp_path: Path) -> None:
    result = execute_multipart_plan(
        _part_plan(),
        resolved_urls=[
            "https://example.com/part1",
            "https://example.com/part2",
            "https://example.com/patch",
        ],
        destination_root=tmp_path,
        downloader=_multipart_downloader,
        unpacker=_multipart_unpacker,
    )
    assert result.failures == ()
    assert [r.final_dir.name for r in result.completed] == ["Part 1", "Part 2"]
    assert (result.version_root / "Part 1" / "game").is_dir()
    assert (result.version_root / "Part 2" / "game").is_dir()


def test_multipart_requires_part_label_on_every_game(tmp_path: Path) -> None:
    plan = _part_plan()
    unlabeled = PlannedArtifact(**{**plan.artifacts[0].__dict__, "part": None})
    plan = DownloadPlan(plan.game, (unlabeled, *plan.artifacts[1:]))
    with pytest.raises(ValueError, match="requires part labels"):
        execute_multipart_plan(
            plan,
            resolved_urls=[
                "https://example.com/part1",
                "https://example.com/part2",
                "https://example.com/patch",
            ],
            destination_root=tmp_path,
        )


def test_multipart_second_part_failure_keeps_the_first(tmp_path: Path) -> None:
    def downloader(url: str, destination: Path) -> list[Path]:
        if "part2" in url:
            raise RuntimeError("mirror down")
        return _multipart_downloader(url, destination)

    result = execute_multipart_plan(
        _part_plan(),
        resolved_urls=[
            "https://example.com/part1",
            "https://example.com/part2",
            "https://example.com/patch",
        ],
        destination_root=tmp_path,
        downloader=downloader,
        unpacker=_multipart_unpacker,
    )
    assert len(result.completed) == 1
    assert result.completed[0].final_dir.name == "Part 1"
    assert result.failures[0].part == "Part 2"
    assert (result.version_root / "Part 1" / "game").is_dir()


def test_multipart_on_part_complete_fires_per_part(tmp_path: Path) -> None:
    seen: list[str] = []
    execute_multipart_plan(
        _part_plan(),
        resolved_urls=[
            "https://example.com/part1",
            "https://example.com/part2",
            "https://example.com/patch",
        ],
        destination_root=tmp_path,
        downloader=_multipart_downloader,
        unpacker=_multipart_unpacker,
        on_part_complete=lambda part, _res: seen.append(part),
    )
    assert seen == ["Part 1", "Part 2"]


def test_multipart_refetch_replaces_only_the_chosen_part(tmp_path: Path) -> None:
    def make_unpacker(marker: bytes) -> object:
        def unpacker(downloaded: list[Path], destination: Path) -> None:
            destination.mkdir(parents=True)
            (destination / "script.rpyc").write_bytes(marker)

        return unpacker

    plan = _part_plan()
    result = execute_multipart_plan(
        plan,
        resolved_urls=[
            "https://example.com/part1",
            "https://example.com/part2",
            "https://example.com/patch",
        ],
        destination_root=tmp_path,
        downloader=_multipart_downloader,
        unpacker=make_unpacker(b"v1"),
    )
    part1_before = (
        result.version_root / "Part 1" / "game" / "script.rpyc"
    ).read_bytes()

    part2_only = DownloadPlan(
        plan.game,
        tuple(artifact for artifact in plan.artifacts if artifact.part != "Part 1"),
    )
    refetch_result = execute_multipart_plan(
        part2_only,
        resolved_urls=["https://example.com/part2-new", "https://example.com/patch"],
        destination_root=tmp_path,
        downloader=_multipart_downloader,
        unpacker=make_unpacker(b"v2"),
    )

    assert refetch_result.version_root == result.version_root
    assert (
        result.version_root / "Part 1" / "game" / "script.rpyc"
    ).read_bytes() == part1_before
    assert (
        result.version_root / "Part 2" / "game" / "script.rpyc"
    ).read_bytes() == b"v2"
