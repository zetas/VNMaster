"""Scanner output records. Pure pydantic dataclasses."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class PlayHistoryEntry(BaseModel):
    save_dir_name: str
    save_dir_path: Path
    last_played_at: int | None  # epoch seconds; None if no .save files
    first_played_at: int | None
    save_count: int
    total_save_size_bytes: int
    persistent_data_present: bool
    # Game version (config.version) read from the most-recent .save file's
    # embedded json metadata. None if no saves or version absent.
    last_played_version: str | None = None


class InstalledGame(BaseModel):
    folder_name: str
    install_path: Path
    installed_version: str  # "unknown" if undetectable
    save_dir_hint: str | None  # from config.save_directory parsing
    disk_size_bytes: int
    launcher_name: str  # e.g., "MyGame.app"
