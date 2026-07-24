"""Path resolution for VNMaster.

macOS-only for v1.0. All paths derive from $HOME.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VNMasterPaths:
    games_root: Path
    renpy_saves_root: Path
    f95checker_db: Path
    vnmaster_db: Path
    config_dir: Path
    log_dir: Path

    @classmethod
    def defaults_for_macos(cls) -> "VNMasterPaths":
        home = Path(os.environ["HOME"])
        return cls(
            games_root=home / "Games",
            renpy_saves_root=home / "Library" / "RenPy",
            f95checker_db=home
            / "Library"
            / "Application Support"
            / "f95checker"
            / "db.sqlite3",
            vnmaster_db=home
            / "Library"
            / "Application Support"
            / "VNMaster"
            / "vnmaster.db",
            config_dir=home / ".config" / "vnmaster",
            log_dir=home / "Library" / "Logs" / "VNMaster",
        )

    @staticmethod
    def expand_user(p: Path, home: Path | None = None) -> Path:
        home = home or Path(os.environ["HOME"])
        s = str(p)
        if s.startswith("~/"):
            return home / s[2:]
        if s == "~":
            return home
        return p
