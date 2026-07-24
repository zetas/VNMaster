"""Merge selected Ren'Py mods and patches into an extracted game."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from vnmaster.downloads.models import PlannedArtifact
from vnmaster.downloads.renpy import RenPyLayoutError, find_renpy_game_dir


_INSTALLABLE_RE = re.compile(
    r"\b(?:multi\s*-?\s*mod|mod|patch|hotfix|fix|cheat|gallery|unlock(?:er)?|"
    r"translation|uncensor(?:ed)?)\b",
    re.I,
)
_README_RE = re.compile(
    r"^(?:read[ _-]?me|install(?:ation)?|instructions?)(?:[ ._-].*)?$",
    re.I,
)
_README_SUFFIXES = frozenset({"", ".txt", ".md", ".rtf", ".pdf"})
_ROOT_INSTRUCTION_RE = re.compile(
    r"""
    \b(?:copy|extract|place|move|install|drop)\b
    [^\n]{0,160}
    \b(?:root|main|base|installation)\s+(?:game\s+)?(?:folder|directory)\b
    |
    \bwhere\s+(?:the\s+)?(?:game\s+)?(?:exe|executable)\b
    """,
    re.I | re.X,
)


class AddonInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class AddonInstallResult:
    target_dir: Path
    files_installed: int
    files_overwritten: int
    readme: Path | None


@dataclass(frozen=True)
class AddonInstallPreview:
    source_root: Path
    target_dir: Path
    files_to_install: int
    files_to_overwrite: int
    readme: Path | None


def should_install_addon(artifact: PlannedArtifact) -> bool:
    """Return whether an optional artifact is a game-modifying add-on."""
    if artifact.kind != "addon":
        return False
    searchable = f"{artifact.title} {artifact.group_name}"
    return bool(_INSTALLABLE_RE.search(searchable))


def install_addon(
    addon_root: Path,
    game_root: Path,
    *,
    platform: str | None,
) -> AddonInstallResult:
    """Merge one extracted add-on into the selected Ren'Py build."""
    preview = preview_addon(addon_root, game_root, platform=platform)
    return apply_addon_preview(preview)


def apply_addon_preview(preview: AddonInstallPreview) -> AddonInstallResult:
    """Apply a previously inspected add-on merge."""
    installed, overwritten = _merge_tree(preview.source_root, preview.target_dir)
    if installed == 0:
        raise AddonInstallError(
            f"Add-on contains no installable files: {preview.source_root}"
        )
    return AddonInstallResult(
        preview.target_dir,
        installed,
        overwritten,
        preview.readme,
    )


def preview_addon(
    addon_root: Path,
    game_root: Path,
    *,
    platform: str | None,
) -> AddonInstallPreview:
    """Resolve an add-on merge without changing the extracted game."""
    try:
        game_dir = find_renpy_game_dir(game_root, platform=platform)
    except RenPyLayoutError as exc:
        raise AddonInstallError(str(exc)) from exc
    if game_dir is None:
        raise AddonInstallError(
            "Could not find a Ren'Py game directory for add-on installation"
        )

    readme = _find_readme(addon_root)
    packaged_game_dir = _find_packaged_game_dir(addon_root)
    if packaged_game_dir is not None:
        source_root = packaged_game_dir
        target_dir = game_dir
    else:
        source_root = _unwrap_single_directory(addon_root)
        target_dir = (
            game_dir.parent
            if readme is not None and _requests_distribution_root(readme)
            else game_dir
        )

    installed, overwritten = _count_merge(source_root, target_dir)
    if installed == 0:
        raise AddonInstallError(f"Add-on contains no installable files: {addon_root}")
    return AddonInstallPreview(
        source_root,
        target_dir,
        installed,
        overwritten,
        readme,
    )


def _find_packaged_game_dir(addon_root: Path) -> Path | None:
    candidates = [
        path
        for path in addon_root.rglob("*")
        if path.is_dir()
        and path.name.casefold() == "game"
        and any(child.is_file() for child in path.rglob("*"))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda path: (len(path.relative_to(addon_root).parts), str(path)))
    shallowest_depth = len(candidates[0].relative_to(addon_root).parts)
    shallowest = [
        path
        for path in candidates
        if len(path.relative_to(addon_root).parts) == shallowest_depth
    ]
    if len(shallowest) == 1:
        return shallowest[0]
    choices = ", ".join(str(path.relative_to(addon_root)) for path in shallowest)
    raise AddonInstallError(
        f"Add-on contains multiple possible game directories: {choices}"
    )


def _find_readme(addon_root: Path) -> Path | None:
    candidates = [
        path
        for path in addon_root.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in _README_SUFFIXES
        and _README_RE.match(path.stem)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda path: (len(path.relative_to(addon_root).parts), str(path)),
    )


def _requests_distribution_root(readme: Path) -> bool:
    if readme.suffix.casefold() not in {"", ".txt", ".md", ".rtf"}:
        return False
    try:
        with readme.open("r", encoding="utf-8", errors="ignore") as source:
            text = source.read(1_000_000)
    except OSError as exc:
        raise AddonInstallError(f"Could not read add-on instructions: {readme}") from exc
    return bool(_ROOT_INSTRUCTION_RE.search(text))


def _unwrap_single_directory(addon_root: Path) -> Path:
    payload = [
        path
        for path in addon_root.iterdir()
        if not _ignore_path(path) and not _is_readme(path)
    ]
    return payload[0] if len(payload) == 1 and payload[0].is_dir() else addon_root


def _merge_tree(source_root: Path, target_root: Path) -> tuple[int, int]:
    installed = 0
    overwritten = 0
    sources = sorted(
        source_root.rglob("*"),
        key=lambda path: (len(path.relative_to(source_root).parts), str(path)),
    )
    target_root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        relative = source.relative_to(source_root)
        if _ignore_relative(relative) or _is_readme(source):
            continue
        if source.is_symlink():
            raise AddonInstallError(f"Add-on contains a symbolic link: {relative}")
        target = target_root / relative
        if source.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                target.unlink()
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not source.is_file():
            raise AddonInstallError(f"Add-on contains an unsupported file: {relative}")
        existed = target.exists() or target.is_symlink()
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        installed += 1
        overwritten += int(existed)
    return installed, overwritten


def _count_merge(source_root: Path, target_root: Path) -> tuple[int, int]:
    installed = 0
    overwritten = 0
    for source in source_root.rglob("*"):
        relative = source.relative_to(source_root)
        if _ignore_relative(relative) or _is_readme(source) or source.is_dir():
            continue
        if source.is_symlink():
            raise AddonInstallError(f"Add-on contains a symbolic link: {relative}")
        if not source.is_file():
            raise AddonInstallError(f"Add-on contains an unsupported file: {relative}")
        target = target_root / relative
        installed += 1
        overwritten += int(target.exists() or target.is_symlink())
    return installed, overwritten


def _ignore_relative(path: Path) -> bool:
    return any(part == "__MACOSX" for part in path.parts) or path.name == ".DS_Store"


def _ignore_path(path: Path) -> bool:
    return path.name in {"__MACOSX", ".DS_Store"}


def _is_readme(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.casefold() in _README_SUFFIXES
        and bool(_README_RE.match(path.stem))
    )
