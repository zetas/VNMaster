"""Scan ~/Games/ for installed Ren'Py games.

A folder is a Ren'Py install if it contains:
  - renpy/ subdir
  - game/ subdir
  - a launcher: *.app, *.sh, or *.exe
"""
from __future__ import annotations

import re
from pathlib import Path

from vnmaster.logging_setup import get_logger
from vnmaster.scanners.types import InstalledGame

log = get_logger(__name__)

_VERSION_FROM_FOLDER_RE = re.compile(r"[-_ ](\d+(?:\.\d+)+[a-z]?)\b")
_CONFIG_VERSION_RE = re.compile(r'config\.version\s*=\s*"([^"]+)"')
_SAVE_DIR_RE = re.compile(r'config\.save_directory\s*=\s*"([^"]+)"')


def scan_disk(root: Path) -> list[InstalledGame]:
    if not root.exists() or not root.is_dir():
        return []

    out: list[InstalledGame] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not _looks_like_renpy(child):
            continue
        try:
            out.append(_game_for(child))
        except (PermissionError, OSError) as e:
            log.warning("could not scan %s: %s", child, e)
    return out


def _looks_like_renpy(dirpath: Path) -> bool:
    return (
        (dirpath / "renpy").is_dir()
        and (dirpath / "game").is_dir()
        and _find_launcher(dirpath) is not None
    )


def _find_launcher(dirpath: Path) -> Path | None:
    for f in dirpath.iterdir():
        name = f.name
        if name.endswith(".app") or name.endswith(".sh") or name.endswith(".exe"):
            return f
    return None


def _game_for(dirpath: Path) -> InstalledGame:
    options = dirpath / "game" / "options.rpy"
    options_text = options.read_text(errors="ignore") if options.exists() else ""

    version = _extract_version(dirpath.name, options_text)
    save_hint = _extract_save_dir_hint(options_text)
    launcher = _find_launcher(dirpath)
    if launcher is None:
        raise FileNotFoundError(f"Ren'Py launcher disappeared while scanning {dirpath}")

    return InstalledGame(
        folder_name=dirpath.name,
        install_path=dirpath,
        installed_version=version,
        save_dir_hint=save_hint,
        disk_size_bytes=_dir_size(dirpath),
        launcher_name=launcher.name,
    )


def _extract_version(folder_name: str, options_text: str) -> str:
    m = _VERSION_FROM_FOLDER_RE.search(folder_name)
    if m:
        return m.group(1)
    m = _CONFIG_VERSION_RE.search(options_text)
    if m:
        return m.group(1)
    return "unknown"


def _extract_save_dir_hint(options_text: str) -> str | None:
    m = _SAVE_DIR_RE.search(options_text)
    return m.group(1) if m else None


def _dir_size(dirpath: Path) -> int:
    total = 0
    for p in dirpath.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total
