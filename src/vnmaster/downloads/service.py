"""Transactional download/extract/publish workflow."""
from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vnmaster.downloads.addon_installer import (
    AddonInstallResult,
    apply_addon_preview,
    preview_addon,
    should_install_addon,
)
from vnmaster.downloads.archives import unpack_payload
from vnmaster.downloads.downloader import download_url
from vnmaster.downloads.models import DownloadPlan, PlannedArtifact, ResolvedDownload
from vnmaster.downloads.urm import install_urm_mod
from vnmaster.downloads.verification import verify_install


class DestinationExistsError(RuntimeError):
    pass


class ArtifactDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class AddonMergeExecution:
    source_path: Path
    target_path: Path
    files_installed: int
    files_overwritten: int
    readme_path: Path | None


@dataclass(frozen=True)
class ArtifactExecution:
    artifact: PlannedArtifact
    download: ResolvedDownload
    output_path: Path
    archive_paths: tuple[Path, ...]
    addon_merge: AddonMergeExecution | None = None


@dataclass(frozen=True)
class DownloadExecutionResult:
    final_dir: Path
    artifacts: tuple[ArtifactExecution, ...]
    verification_checks: tuple[str, ...]
    renpy_game_dir: Path | None
    urm_path: Path | None


def execute_download_plan(
    plan: DownloadPlan,
    *,
    resolved_downloads: list[tuple[ResolvedDownload, ...]] | None = None,
    resolved_urls: list[str] | None = None,
    destination_root: Path,
    urm_mods_dir: Path | None = None,
    downloader: Callable[[str, Path], list[Path]] = download_url,
    unpacker: Callable[[list[Path], Path], None] = unpack_payload,
    reporter: Callable[[str], None] = lambda _message: None,
) -> Path:
    return execute_download_plan_detailed(
        plan,
        resolved_downloads=resolved_downloads,
        resolved_urls=resolved_urls,
        destination_root=destination_root,
        urm_mods_dir=urm_mods_dir,
        downloader=downloader,
        unpacker=unpacker,
        reporter=reporter,
    ).final_dir


def execute_download_plan_detailed(
    plan: DownloadPlan,
    *,
    resolved_downloads: list[tuple[ResolvedDownload, ...]] | None = None,
    resolved_urls: list[str] | None = None,
    destination_root: Path,
    urm_mods_dir: Path | None = None,
    downloader: Callable[[str, Path], list[Path]] = download_url,
    unpacker: Callable[[list[Path], Path], None] = unpack_payload,
    reporter: Callable[[str], None] = lambda _message: None,
) -> DownloadExecutionResult:
    candidates = _normalize_resolved_downloads(
        plan, resolved_downloads=resolved_downloads, resolved_urls=resolved_urls
    )

    version = _safe_component(plan.game.version or "unknown-version")
    final_dir = destination_root / _safe_component(plan.game.title) / version
    return _execute_pairs(
        list(zip(plan.artifacts, candidates, strict=True)),
        final_dir=final_dir,
        staging_parent=destination_root,
        urm_mods_dir=urm_mods_dir,
        downloader=downloader,
        unpacker=unpacker,
        reporter=reporter,
        replace_existing=False,
    )


def _execute_pairs(
    pairs: list[tuple[PlannedArtifact, tuple[ResolvedDownload, ...]]],
    *,
    final_dir: Path,
    staging_parent: Path,
    urm_mods_dir: Path | None,
    downloader: Callable[[str, Path], list[Path]],
    unpacker: Callable[[list[Path], Path], None],
    reporter: Callable[[str], None],
    replace_existing: bool = False,
) -> DownloadExecutionResult:
    if final_dir.exists() and not replace_existing:
        raise DestinationExistsError(
            f"Destination already exists; refusing to overwrite it: {final_dir}"
        )

    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".vnmaster-fetch-", dir=staging_parent))
    published = False
    game_download: ResolvedDownload | None = None
    executions: list[ArtifactExecution] = []
    installable_addons: list[tuple[int, PlannedArtifact, Path]] = []
    try:
        for index, (artifact, artifact_candidates) in enumerate(pairs, start=1):
            if artifact.kind == "game":
                output_dir = staging / "game"
                archive_dir = staging / "archive"
            else:
                output_dir = staging / "addons" / _safe_component(artifact.title)
                archive_dir = (
                    staging
                    / "archive"
                    / "addons"
                    / f"{index:02d}-{_safe_component(artifact.title)}"
                )
            selected, archived = _download_with_fallbacks(
                artifact,
                artifact_candidates,
                index=index,
                staging=staging,
                output_dir=output_dir,
                archive_dir=archive_dir,
                downloader=downloader,
                unpacker=unpacker,
                reporter=reporter,
            )
            executions.append(
                ArtifactExecution(
                    artifact=artifact,
                    download=selected,
                    output_path=output_dir.relative_to(staging),
                    archive_paths=archived,
                )
            )
            if artifact.kind == "game":
                game_download = selected
            elif should_install_addon(artifact):
                installable_addons.append((len(executions) - 1, artifact, output_dir))

        addon_results: list[AddonInstallResult] = []
        for execution_index, artifact, addon_dir in installable_addons:
            preview = preview_addon(
                addon_dir,
                staging / "game",
                platform=game_download.platform if game_download else None,
            )
            reporter(
                f"Add-on install preview for {artifact.title!r}: "
                f"{preview.files_to_install} files "
                f"({preview.files_to_overwrite} overwritten) -> "
                f"{preview.target_dir.relative_to(staging)}"
            )
            result = apply_addon_preview(preview)
            addon_results.append(result)
            readme = (
                f"; instructions: {result.readme.relative_to(addon_dir)}"
                if result.readme is not None
                else "; no README, used standard game-folder merge"
            )
            reporter(
                f"Installed add-on {artifact.title!r}: {result.files_installed} files "
                f"({result.files_overwritten} overwritten){readme}"
            )
            executions[execution_index] = ArtifactExecution(
                artifact=executions[execution_index].artifact,
                download=executions[execution_index].download,
                output_path=executions[execution_index].output_path,
                archive_paths=executions[execution_index].archive_paths,
                addon_merge=AddonMergeExecution(
                    source_path=preview.source_root.relative_to(staging),
                    target_path=result.target_dir.relative_to(staging),
                    files_installed=result.files_installed,
                    files_overwritten=result.files_overwritten,
                    readme_path=(
                        result.readme.relative_to(staging)
                        if result.readme is not None
                        else None
                    ),
                ),
            )

        urm_path: Path | None = None
        if urm_mods_dir is not None:
            installed = install_urm_mod(
                staging / "game",
                urm_mods_dir,
                platform=game_download.platform if game_download else None,
            )
            if installed is None:
                reporter("URM not installed: the extracted build is not a Ren'Py game.")
            else:
                reporter(f"Installed URM: {installed.relative_to(staging)}")
                urm_path = installed.relative_to(staging)

        shutil.rmtree(staging / ".attempts", ignore_errors=True)
        verification = verify_install(
            staging / "game",
            platform=game_download.platform if game_download else None,
            archive_paths=tuple(
                staging / archive
                for execution in executions
                for archive in execution.archive_paths
            ),
            addon_results=tuple(addon_results),
            urm_installed=urm_path is not None,
        )
        for check in verification.checks:
            reporter(f"Verified: {check}")

        final_dir.parent.mkdir(parents=True, exist_ok=True)
        if final_dir.exists():
            if not replace_existing:
                raise DestinationExistsError(
                    f"Destination already exists; refusing to overwrite it: {final_dir}"
                )
            previous = final_dir.parent / f".vnmaster-previous-{staging.name}"
            final_dir.replace(previous)
            staging.replace(final_dir)
            shutil.rmtree(previous)
        else:
            staging.replace(final_dir)
        published = True
        return DownloadExecutionResult(
            final_dir=final_dir,
            artifacts=tuple(executions),
            verification_checks=verification.checks,
            renpy_game_dir=(
                verification.renpy_game_dir.relative_to(staging)
                if verification.renpy_game_dir is not None
                else None
            ),
            urm_path=urm_path,
        )
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


@dataclass(frozen=True)
class PartFailure:
    part: str
    error: str


@dataclass(frozen=True)
class MultiPartExecutionResult:
    version_root: Path
    completed: tuple[DownloadExecutionResult, ...]
    failures: tuple[PartFailure, ...]


def execute_multipart_plan(
    plan: DownloadPlan,
    *,
    resolved_downloads: list[tuple[ResolvedDownload, ...]] | None = None,
    resolved_urls: list[str] | None = None,
    destination_root: Path,
    urm_mods_dir: Path | None = None,
    downloader: Callable[[str, Path], list[Path]] = download_url,
    unpacker: Callable[[list[Path], Path], None] = unpack_payload,
    reporter: Callable[[str], None] = lambda _message: None,
    on_part_complete: Callable[[str, DownloadExecutionResult], None] | None = None,
) -> MultiPartExecutionResult:
    candidates = _normalize_resolved_downloads(
        plan, resolved_downloads=resolved_downloads, resolved_urls=resolved_urls
    )
    pairs = list(zip(plan.artifacts, candidates, strict=True))
    games = [(a, c) for a, c in pairs if a.kind == "game"]
    if any(artifact.part is None for artifact, _ in games):
        raise ValueError("execute_multipart_plan requires part labels on every game")
    tagged = [(a, c) for a, c in pairs if a.kind == "addon" and a.part is not None]
    shared = [(a, c) for a, c in pairs if a.kind == "addon" and a.part is None]

    version = _safe_component(plan.game.version or "unknown-version")
    version_root = destination_root / _safe_component(plan.game.title) / version
    version_root.mkdir(parents=True, exist_ok=True)

    completed: list[DownloadExecutionResult] = []
    failures: list[PartFailure] = []
    for artifact, artifact_candidates in games:
        assert artifact.part is not None  # guarded above: every game has a part label
        part_label = artifact.part
        part_pairs = [
            (artifact, artifact_candidates),
            *[(a, c) for a, c in tagged if a.part == part_label],
            *shared,
        ]
        part_dir = version_root / _safe_component(part_label)
        reporter(f"Fetching {part_label} into {part_dir}...")
        try:
            result = _execute_pairs(
                part_pairs,
                final_dir=part_dir,
                staging_parent=destination_root,
                urm_mods_dir=urm_mods_dir,
                downloader=downloader,
                unpacker=unpacker,
                reporter=reporter,
                replace_existing=True,
            )
        except (ArtifactDownloadError, RuntimeError, OSError) as exc:
            detail = _concise_error(exc)
            failures.append(PartFailure(part_label, detail))
            reporter(f"{part_label} failed: {detail}")
            continue
        completed.append(result)
        if on_part_complete is not None:
            on_part_complete(part_label, result)
    return MultiPartExecutionResult(
        version_root=version_root,
        completed=tuple(completed),
        failures=tuple(failures),
    )


def _normalize_resolved_downloads(
    plan: DownloadPlan,
    *,
    resolved_downloads: list[tuple[ResolvedDownload, ...]] | None,
    resolved_urls: list[str] | None,
) -> list[tuple[ResolvedDownload, ...]]:
    if resolved_downloads is not None and resolved_urls is not None:
        raise ValueError("Provide resolved_downloads or resolved_urls, not both")
    if resolved_downloads is None:
        if resolved_urls is None:
            raise ValueError("Resolved downloads are required")
        if len(resolved_urls) != len(plan.artifacts):
            raise ValueError("One resolved URL is required for each planned artifact")
        resolved_downloads = [
            (
                ResolvedDownload(
                    artifact.host,
                    artifact.locator,
                    url,
                    platform=artifact.platform,
                    group_name=artifact.group_name,
                ),
            )
            for artifact, url in zip(plan.artifacts, resolved_urls, strict=True)
        ]
    if len(resolved_downloads) != len(plan.artifacts):
        raise ValueError("Resolved candidates are required for each planned artifact")
    if any(not artifact_candidates for artifact_candidates in resolved_downloads):
        raise ValueError("Each planned artifact requires at least one resolved candidate")
    return resolved_downloads


def _download_with_fallbacks(
    artifact: PlannedArtifact,
    candidates: tuple[ResolvedDownload, ...],
    *,
    index: int,
    staging: Path,
    output_dir: Path,
    archive_dir: Path,
    downloader: Callable[[str, Path], list[Path]],
    unpacker: Callable[[list[Path], Path], None],
    reporter: Callable[[str], None],
) -> tuple[ResolvedDownload, tuple[Path, ...]]:
    failures: list[str] = []
    for candidate_index, candidate in enumerate(candidates, start=1):
        label = _candidate_label(candidate)
        reporter(
            f"Downloading {artifact.title!r} via {label} "
            f"({candidate_index}/{len(candidates)})..."
        )
        attempt_root = (
            staging / ".attempts" / f"{index:02d}" / f"{candidate_index:02d}"
        )
        try:
            downloaded = downloader(candidate.url, attempt_root / "download")
            unpacker(downloaded, attempt_root / "output")
            archive_dir.mkdir(parents=True)
            archived: list[Path] = []
            for payload in downloaded:
                destination = archive_dir / payload.name
                payload.replace(destination)
                archived.append(destination.relative_to(staging))
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            (attempt_root / "output").replace(output_dir)
            shutil.rmtree(staging / ".attempts" / f"{index:02d}", ignore_errors=True)
            return candidate, tuple(archived)
        except (RuntimeError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            detail = _concise_error(exc)
            failures.append(f"{label}: {detail}")
            reporter(f"{label} failed: {detail}")
            shutil.rmtree(attempt_root, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)
            shutil.rmtree(archive_dir, ignore_errors=True)

    detail = "; ".join(failures)
    raise ArtifactDownloadError(
        f"All {len(candidates)} mirrors failed for {artifact.title!r}: {detail}"
    )


def _concise_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return message[:500] or type(exc).__name__


def _candidate_label(candidate: ResolvedDownload) -> str:
    return (
        f"{candidate.host} [{candidate.platform}]"
        if candidate.platform
        else candidate.host
    )


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^\w. -]+", "-", value, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return cleaned[:120] or "unnamed"
