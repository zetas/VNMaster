"""Locate the writable ``game`` directory in extracted Ren'Py builds."""
from __future__ import annotations

from pathlib import Path


_RENPY_SCRIPT_SUFFIXES = frozenset({".rpa", ".rpy", ".rpyc"})


class RenPyLayoutError(RuntimeError):
    pass


def find_renpy_game_dir(game_root: Path, *, platform: str | None) -> Path | None:
    """Return the active Ren'Py ``game`` directory for an extracted build."""
    candidates = [
        path
        for path in (game_root, *game_root.rglob("game"))
        if path.is_dir() and _contains_renpy_scripts(path)
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    app_candidates = [path for path in candidates if _is_macos_app_game_dir(path)]
    if platform == "mac" and len(app_candidates) == 1:
        return app_candidates[0]

    non_app_candidates = [path for path in candidates if path not in app_candidates]
    if platform in {"windows", "linux"} and len(non_app_candidates) == 1:
        return non_app_candidates[0]

    choices = ", ".join(str(path.relative_to(game_root)) for path in candidates)
    raise RenPyLayoutError(
        f"Found multiple Ren'Py game directories and could not choose one: {choices}"
    )


def _contains_renpy_scripts(path: Path) -> bool:
    try:
        return any(
            child.is_file() and child.suffix.casefold() in _RENPY_SCRIPT_SUFFIXES
            for child in path.iterdir()
        )
    except OSError as exc:
        raise RenPyLayoutError(
            f"Could not inspect extracted game directory {path}"
        ) from exc


def _is_macos_app_game_dir(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    return (
        len(parts) >= 5
        and parts[-4:] == ("contents", "resources", "autorun", "game")
        and parts[-5].endswith(".app")
    )
