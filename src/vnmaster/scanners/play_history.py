"""Scan ~/Library/RenPy/ for save data.

Each subdirectory is treated as a played-game record, even if the game is no
longer installed on disk.

A game's `config.save_directory` can itself be a path ("Talothral/Sorcerer2"),
so saves also turn up one level down. Those nested dirs are reported under their
path relative to the root, which keeps them distinct from a same-named top-level
dir. Ren'Py's own `sync/` mirror is skipped: it duplicates its parent's saves,
and almost every save folder has one.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from vnmaster.logging_setup import get_logger
from vnmaster.scanners.types import PlayHistoryEntry

log = get_logger(__name__)

# Ren'Py's cloud-sync staging dir, a copy of the parent's saves.
_MIRROR_DIRS = {"sync"}


def scan_play_history(root: Path) -> list[PlayHistoryEntry]:
    if not root.exists() or not root.is_dir():
        return []

    out: list[PlayHistoryEntry] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            out.append(_entry_for(child))
            out.extend(_nested_entries(child))
        except PermissionError as e:
            log.warning("permission denied scanning %s: %s", child, e)
            continue
    return out


def _nested_entries(parent: Path) -> list[PlayHistoryEntry]:
    """Save dirs one level below a top-level folder.

    Only dirs that actually hold saves count, so asset/persistent subfolders
    stay out of the library.
    """
    out: list[PlayHistoryEntry] = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.name in _MIRROR_DIRS:
            continue
        try:
            if not any(child.glob("*.save")):
                continue
            out.append(_entry_for(child, name=f"{parent.name}/{child.name}"))
        except PermissionError as e:
            log.warning("permission denied scanning %s: %s", child, e)
    return out


def _entry_for(dirpath: Path, name: str | None = None) -> PlayHistoryEntry:
    save_files = list(dirpath.glob("*.save"))
    persistent = dirpath / "persistent"

    last_played: int | None = None
    first_played: int | None = None
    total_size = 0
    last_played_version: str | None = None

    if save_files:
        # Sort by mtime; newest save reflects the version last played.
        save_files_by_mtime = sorted(
            save_files, key=lambda s: s.stat().st_mtime
        )
        mtimes = [int(s.stat().st_mtime) for s in save_files]
        last_played = max(mtimes)
        first_played = min(mtimes)
        last_played_version = _read_save_version(save_files_by_mtime[-1])

    for f in dirpath.iterdir():
        if f.is_file():
            try:
                total_size += f.stat().st_size
            except OSError:
                continue

    return PlayHistoryEntry(
        save_dir_name=name or dirpath.name,
        save_dir_path=dirpath,
        last_played_at=last_played,
        first_played_at=first_played,
        save_count=len(save_files),
        total_save_size_bytes=total_size,
        persistent_data_present=persistent.exists(),
        last_played_version=last_played_version,
    )


def _read_save_version(save_file: Path) -> str | None:
    """Extract the game's config.version from a Ren'Py .save file.

    Ren'Py saves are ZIP archives containing a `json` member with a
    `_version` key holding the game's config.version at save time. Returns
    None if the file isn't a valid save, has no json member, or no _version.
    """
    try:
        with zipfile.ZipFile(save_file) as z:
            if "json" not in z.namelist():
                return None
            meta = json.loads(z.read("json"))
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, OSError) as e:
        log.debug("could not read version from %s: %s", save_file, e)
        return None
    version = meta.get("_version")
    if version is None:
        return None
    return str(version)
