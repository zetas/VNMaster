"""Install user-supplied Ren'Py patches and mods into recorded games."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine

from vnmaster.downloads.addon_installer import (
    AddonInstallPreview,
    AddonInstallResult,
    addon_merge_paths,
    apply_addon_preview,
    preview_addon_for_game_dir,
)
from vnmaster.downloads.archives import unpack_payload
from vnmaster.downloads.renpy import find_renpy_game_dirs
from vnmaster.downloads.state import (
    InstallArtifactAddition,
    InstallState,
    append_install_artifacts,
    list_install_states,
)


class LocalAddonError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalAddonTarget:
    id: str
    label: str
    state: InstallState
    scope_root: Path
    game_root: Path
    renpy_game_dir: Path
    renpy_relative: Path
    part: str | None
    platform: str | None


@dataclass(frozen=True)
class LocalAddonInventory:
    targets: tuple[LocalAddonTarget, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PreparedLocalAddon:
    source: Path
    extracted_root: Path
    name: str


@dataclass(frozen=True)
class LocalAddonTargetPlan:
    target: LocalAddonTarget
    preview: AddonInstallPreview


@dataclass(frozen=True)
class LocalAddonPlan:
    prepared: PreparedLocalAddon
    targets: tuple[LocalAddonTargetPlan, ...]

    @property
    def files_to_install(self) -> int:
        return sum(item.preview.files_to_install for item in self.targets)

    @property
    def files_to_overwrite(self) -> int:
        return sum(item.preview.files_to_overwrite for item in self.targets)


@dataclass(frozen=True)
class LocalAddonTargetResult:
    target: LocalAddonTarget
    merge: AddonInstallResult
    archive_path: Path
    backup_path: Path


@dataclass(frozen=True)
class LocalAddonResult:
    name: str
    targets: tuple[LocalAddonTargetResult, ...]


@dataclass(frozen=True)
class _UndoEntry:
    target: Path
    backup: Path | None


def discover_local_addon_targets(engine: Engine) -> LocalAddonInventory:
    """Expand recorded installs into individual Ren'Py app/game targets."""
    targets: list[LocalAddonTarget] = []
    warnings: list[str] = []
    for state in list_install_states(engine):
        scopes = _install_scopes(state)
        if not state.install_path.is_dir():
            warnings.append(
                f"{state.game_title} {state.version or 'unknown'}: "
                f"recorded path is missing ({state.install_path})"
            )
            continue
        for part, scope_root, platform in scopes:
            game_root = scope_root / "game"
            if not game_root.is_dir():
                suffix = f" {part}" if part else ""
                warnings.append(
                    f"{state.game_title}{suffix}: game payload is missing ({game_root})"
                )
                continue
            game_dirs = find_renpy_game_dirs(game_root)
            if not game_dirs:
                suffix = f" {part}" if part else ""
                warnings.append(f"{state.game_title}{suffix}: no Ren'Py game directory found")
                continue
            for game_dir in game_dirs:
                relative = game_dir.relative_to(game_root)
                target_id = f"{state.id}:{part or 'main'}:{relative.as_posix() or '.'}"
                targets.append(
                    LocalAddonTarget(
                        id=target_id,
                        label=_target_label(state, part, game_dir, relative),
                        state=state,
                        scope_root=scope_root,
                        game_root=game_root,
                        renpy_game_dir=game_dir,
                        renpy_relative=relative,
                        part=part,
                        platform=platform,
                    )
                )
    return LocalAddonInventory(tuple(targets), tuple(warnings))


@contextmanager
def prepare_local_addon(
    source: Path,
    *,
    name: str | None = None,
) -> Iterator[PreparedLocalAddon]:
    """Safely unpack a local source into temporary staging for inspection."""
    expanded_source = source.expanduser()
    if expanded_source.is_symlink():
        raise LocalAddonError(
            f"Local add-on source cannot be a symbolic link: {expanded_source}"
        )
    source = expanded_source.resolve()
    if not source.exists():
        raise LocalAddonError(f"Local add-on source does not exist: {source}")
    if not source.is_file() and not source.is_dir():
        raise LocalAddonError(f"Unsupported local add-on source: {source}")
    if source.is_dir():
        symbolic_links = [path for path in source.rglob("*") if path.is_symlink()]
        if symbolic_links:
            raise LocalAddonError(
                f"Local add-on source contains a symbolic link: {symbolic_links[0]}"
            )
    display_name = (name or _source_name(source)).strip()
    if not display_name:
        raise LocalAddonError("Local add-on name cannot be empty")

    with tempfile.TemporaryDirectory(prefix="vnmaster-local-addon-") as temporary:
        extracted_root = Path(temporary) / "payload"
        try:
            unpack_payload([source], extracted_root)
        except Exception as exc:
            raise LocalAddonError(f"Could not unpack local add-on {source}: {exc}") from exc
        yield PreparedLocalAddon(source, extracted_root, display_name)


def preview_local_addon(
    prepared: PreparedLocalAddon,
    targets: tuple[LocalAddonTarget, ...],
) -> LocalAddonPlan:
    if not targets:
        raise LocalAddonError("Select at least one game target")
    plans = tuple(
        LocalAddonTargetPlan(
            target,
            preview_addon_for_game_dir(prepared.extracted_root, target.renpy_game_dir),
        )
        for target in targets
    )
    return LocalAddonPlan(prepared, plans)


def install_local_addon(engine: Engine, plan: LocalAddonPlan) -> LocalAddonResult:
    """Preserve, merge, and record a previewed local add-on transactionally."""
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}"
    component = _safe_component(plan.prepared.name)
    archive_roots: list[Path] = []
    backup_roots: list[Path] = []
    preserved_by_scope: dict[tuple[int, Path], Path] = {}
    undo_by_plan: list[tuple[LocalAddonTargetPlan, tuple[_UndoEntry, ...]]] = []
    results: list[LocalAddonTargetResult] = []

    try:
        for item in plan.targets:
            scope_key = (item.target.state.id, item.target.scope_root)
            preserved = preserved_by_scope.get(scope_key)
            if preserved is None:
                archive_root = (
                    item.target.scope_root
                    / "archive"
                    / "local-addons"
                    / f"{stamp}-{component}"
                )
                archive_roots.append(archive_root)
                preserved = _preserve_source(plan.prepared.source, archive_root)
                preserved_by_scope[scope_key] = preserved

            target_digest = hashlib.sha256(item.target.id.encode()).hexdigest()[:8]
            target_component = f"{_safe_component(item.target.id)}-{target_digest}"
            backup_root = (
                item.target.scope_root
                / "backups"
                / "local-addons"
                / f"{stamp}-{component}"
                / target_component
            )
            backup_roots.append(backup_root)
            undo = _snapshot_preview(item.preview, backup_root)
            undo_by_plan.append((item, undo))
            merge = apply_addon_preview(item.preview)
            results.append(
                LocalAddonTargetResult(item.target, merge, preserved, backup_root)
            )

        additions = tuple(
            _state_addition(plan.prepared.name, result)
            for result in results
        )
        append_install_artifacts(engine, additions)
    except Exception as exc:
        for _item, undo in reversed(undo_by_plan):
            _restore_snapshot(undo)
        for root in reversed(archive_roots):
            shutil.rmtree(root, ignore_errors=True)
        for root in reversed(backup_roots):
            shutil.rmtree(root, ignore_errors=True)
        for batch_root in {root.parent for root in backup_roots}:
            shutil.rmtree(batch_root, ignore_errors=True)
        if isinstance(exc, LocalAddonError):
            raise
        raise LocalAddonError(f"Local add-on installation failed and was rolled back: {exc}") from exc

    return LocalAddonResult(plan.prepared.name, tuple(results))


def _install_scopes(state: InstallState) -> tuple[tuple[str | None, Path, str | None], ...]:
    parts = sorted(
        {
            part
            for artifact in state.artifacts
            if isinstance(part := artifact.get("part"), str)
        }
    )
    if not parts:
        return ((None, state.install_path, state.platform),)
    return tuple(
        (part, state.install_path / part, _part_platform(state, part))
        for part in parts
    )


def _part_platform(state: InstallState, part: str) -> str | None:
    for artifact in state.artifacts:
        value = artifact.get("platform")
        if artifact.get("kind") == "game" and artifact.get("part") == part:
            return value if isinstance(value, str) else state.platform
    return state.platform


def _target_label(
    state: InstallState,
    part: str | None,
    game_dir: Path,
    relative: Path,
) -> str:
    details = [state.game_title, state.version or "unknown version"]
    if part:
        details.append(part)
    app = next(
        (parent.name for parent in (game_dir, *game_dir.parents) if parent.suffix == ".app"),
        None,
    )
    payload = app or (relative.as_posix() if str(relative) != "." else "game")
    details.append(payload)
    return " · ".join(details)


def _source_name(source: Path) -> str:
    archive_suffixes = (".tar.gz", ".tar.bz2", ".tar.xz")
    lowered = source.name.casefold()
    for suffix in archive_suffixes:
        if lowered.endswith(suffix):
            return source.name[: -len(suffix)]
    return source.stem if source.is_file() else source.name


def _preserve_source(source: Path, archive_root: Path) -> Path:
    archive_root.mkdir(parents=True, exist_ok=False)
    destination = archive_root / source.name
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)
    return destination


def _snapshot_preview(
    preview: AddonInstallPreview,
    backup_root: Path,
) -> tuple[_UndoEntry, ...]:
    entries: list[_UndoEntry] = []
    seen: set[Path] = set()
    for change in addon_merge_paths(preview):
        target = change.target
        if target in seen:
            continue
        seen.add(target)
        source_is_dir = change.source.is_dir()
        target_is_dir = target.is_dir() and not target.is_symlink()
        needs_snapshot = (
            (not source_is_dir)
            or target.is_symlink()
            or ((target.exists() or target.is_symlink()) and source_is_dir != target_is_dir)
            or not target.exists()
        )
        if not needs_snapshot:
            continue
        existed = target.exists() or target.is_symlink()
        backup: Path | None = None
        if existed:
            relative = target.relative_to(preview.target_dir)
            backup = backup_root / "files" / relative
            _copy_path(target, backup)
        entries.append(_UndoEntry(target, backup))
    backup_root.mkdir(parents=True, exist_ok=True)
    return tuple(entries)


def _restore_snapshot(entries: tuple[_UndoEntry, ...]) -> None:
    for entry in sorted(entries, key=lambda item: len(item.target.parts), reverse=True):
        _remove_path(entry.target)
        if entry.backup is not None:
            _copy_path(entry.backup, entry.target)


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _state_addition(
    name: str,
    result: LocalAddonTargetResult,
) -> InstallArtifactAddition:
    target = result.target
    state = target.state
    archive_relative = result.archive_path.relative_to(state.install_path)
    output_relative = target.renpy_game_dir.relative_to(state.install_path)
    backup_relative = result.backup_path.relative_to(state.install_path)
    artifact: dict[str, object] = {
        "kind": "addon",
        "title": name,
        "version": None,
        "thread_id": state.f95_thread_id,
        "thread_url": state.thread_url,
        "group_name": "Local add-on",
        "platform": target.platform,
        "host": "Local",
        "source_locator": f"local:{result.archive_path.name}",
        "output_path": str(output_relative),
        "archive_paths": [str(archive_relative)],
        "installable": True,
        "local": True,
        "renpy_target": str(target.renpy_relative),
        "backup_path": str(backup_relative),
        "merge": {
            "target_path": str(output_relative),
            "files_installed": result.merge.files_installed,
            "files_overwritten": result.merge.files_overwritten,
        },
    }
    if target.part is not None:
        artifact["part"] = target.part
    prefix = f"{target.part}: " if target.part else ""
    check = (
        f"{prefix}local add-on {name}: {result.merge.files_installed} files installed"
    )
    return InstallArtifactAddition(
        state_id=state.id,
        artifact=artifact,
        archive_path=result.archive_path,
        archive_relative=archive_relative,
        verification_check=check,
    )


def _safe_component(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value.strip()
    ).strip(".-")
    return cleaned[:100] or "local-addon"
