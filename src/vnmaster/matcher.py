"""Join scanner output + F95Checker rows into LibraryGame upserts.

Pure function — takes lists in, returns a MatchResult out. No DB access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from vnmaster.db.ro_f95checker import F95CheckerGame
from vnmaster.magnitude import version_tokens as version_tokens  # explicit re-export
from vnmaster.scanners.types import InstalledGame, PlayHistoryEntry


# Strip noise that drags WRatio scores below the fuzzy threshold:
#   "Eternum-1610153667" → "Eternum"
#   "ATouchofMagic-1691411418" → "ATouchofMagic"
#   "FetishLocator-Week2" → "FetishLocator"
_NOISE_SUFFIXES = re.compile(
    r"(?i)[-_]"
    r"(?:\d{6,}"  # long numeric timestamp
    r"|saves?|savegames?|full|demo|beta|public|rewrite|complete|"
    r"week\d+|ch\d+|chp\d+|chapter\d+|pt\d+|part\d+|ep\d+|episode\d+|s\d+|"
    r"v\d+(?:\.\d+)*|ea\d*"
    r")$"
)

# Directory names the downloader creates for multi-part installs, e.g.
# "Part 1", "Chapter 3" — see downloads/selector.py's _PART_FAMILIES.
_PART_DIR_RE = re.compile(
    r"^(?:Part|Chapter|Episode|Volume|Book|Act|Season) \d+$"
)


def _part_version_root(path: Path) -> Path | None:
    """Version root of a per-part install, or None if path isn't under a part dir."""
    for ancestor in path.parents:
        if _PART_DIR_RE.match(ancestor.name):
            return ancestor.parent
    return None


def _clean_for_match(name: str) -> str:
    """Strip timestamp + dev-suffix noise before fuzzy matching.

    Applied iteratively so chained suffixes like "Foo-Saves-100001" collapse
    cleanly. WRatio between cleaned save_dir_name and F95Checker game title
    is reliably high (≥90) when they're actually the same game.

    A save dir may be a path when the game sets config.save_directory to one
    ("Talothral/Sorcerer2"); only the leaf names the game, and keeping the
    parent drags the score down.
    """
    name = name.rsplit("/", 1)[-1]
    prev = ""
    while name != prev:
        prev = name
        name = _NOISE_SUFFIXES.sub("", name).strip("-_ ")
    return name or prev


@dataclass(frozen=True)
class LibraryMatch:
    f95_thread_id: int
    game_title: str
    save_dir_name: str | None
    last_played_at: int | None
    first_played_at: int | None
    save_count: int | None
    total_save_size_bytes: int | None
    install_path: Path | None
    installed_version: str | None
    disk_size_bytes: int | None


@dataclass(frozen=True)
class LearnedPairing:
    """A match derived by name-fuzzy or version-corroboration, not from cache.

    Callers (the pipeline) should upsert these into the pairings table so that
    the match survives the next run even if the save folder name changes or
    the F95 title drifts.
    """

    f95_thread_id: int
    save_dir_name: str | None
    folder_name: str | None
    confidence: float
    method: str  # "name" | "version"


@dataclass(frozen=True)
class MatchResult:
    matches: list[LibraryMatch]
    unmatched_installed: list[InstalledGame]
    unmatched_play_history: list[PlayHistoryEntry]
    learned_pairings: list[LearnedPairing]


def match_library(
    play_history: list[PlayHistoryEntry],
    installed: list[InstalledGame],
    f95_rows: list[F95CheckerGame],
    cached_pairings: dict[str, int],
    fuzzy_threshold: int,
    corroboration_floor: int = 70,
) -> MatchResult:
    f95_by_id = {f.id: f for f in f95_rows}
    f95_by_name_for_fuzzy: dict[str, int] = {f.name: f.id for f in f95_rows}

    # Index disk entries by save_dir_hint for cross-binding.
    disk_by_hint: dict[str, InstalledGame] = {}
    disk_remaining = list(installed)
    for d in installed:
        if d.save_dir_hint:
            disk_by_hint[d.save_dir_hint] = d

    # Resolve each save dir to an F95 thread.
    save_to_thread: dict[str, int] = {}
    unmatched_saves: list[PlayHistoryEntry] = []
    learned_pairings: list[LearnedPairing] = []

    for save in play_history:
        result = _resolve_save(
            save=save,
            f95_rows=f95_rows,
            f95_by_name=f95_by_name_for_fuzzy,
            cached_pairings=cached_pairings,
            fuzzy_threshold=fuzzy_threshold,
            corroboration_floor=corroboration_floor,
        )
        if result is not None:
            thread_id, method = result
            save_to_thread[save.save_dir_name] = thread_id
            # Only emit a LearnedPairing when the match was NOT from cache.
            if method in ("name", "version"):
                confidence = 0.95 if method == "name" else 0.85
                learned_pairings.append(LearnedPairing(
                    f95_thread_id=thread_id,
                    save_dir_name=save.save_dir_name,
                    folder_name=None,
                    confidence=confidence,
                    method=method,
                ))
        else:
            unmatched_saves.append(save)

    # Resolve each disk entry to an F95 thread (executable path beats fuzzy).
    disk_to_thread: dict[str, int] = {}
    unmatched_disk: list[InstalledGame] = []
    for d in disk_remaining:
        disk_tid = _resolve_disk(d, f95_rows, cached_pairings, fuzzy_threshold)
        if disk_tid is not None:
            disk_to_thread[d.folder_name] = disk_tid
        else:
            unmatched_disk.append(d)

    # Build a per-thread merged record.
    by_thread: dict[int, LibraryMatch] = {}

    for save in play_history:
        tid = save_to_thread.get(save.save_dir_name)
        if tid is None:
            continue
        f95 = f95_by_id[tid]
        by_thread[tid] = LibraryMatch(
            f95_thread_id=tid, game_title=f95.name,
            save_dir_name=save.save_dir_name,
            last_played_at=save.last_played_at,
            first_played_at=save.first_played_at,
            save_count=save.save_count,
            total_save_size_bytes=save.total_save_size_bytes,
            install_path=None,
            # Version the user last played, read from the save file. The
            # disk scanner overrides this below if the game is currently
            # installed (disk version is authoritative when present).
            installed_version=save.last_played_version,
            disk_size_bytes=None,
        )

    for d in installed:
        tid = disk_to_thread.get(d.folder_name)
        if tid is None:
            continue
        # Cross-bind by save_dir_hint when present.
        save_name = d.save_dir_hint
        existing = by_thread.get(tid)
        existing_part_root = (
            _part_version_root(existing.install_path)
            if existing is not None and existing.install_path is not None
            else None
        )
        d_part_root = _part_version_root(d.install_path)
        if (
            existing is not None
            and existing.install_path is not None
            and d_part_root is not None
            and (
                existing_part_root == d_part_root
                or existing.install_path == d_part_root
            )
        ):
            # Both entries sit under a per-part dir with the same version
            # root, or existing is already a merged entry sitting at that
            # version root (3rd+ part arriving): a genuine multi-part
            # install. Aggregate instead of last-one-wins.
            by_thread[tid] = LibraryMatch(
                **{
                    **existing.__dict__,
                    "install_path": d_part_root,
                    "installed_version": (
                        existing.installed_version
                        if existing.installed_version is not None
                        else d.installed_version
                    ),
                    "disk_size_bytes": (existing.disk_size_bytes or 0)
                    + (d.disk_size_bytes or 0),
                }
            )
        elif existing is not None:
            # Either existing came from play history only (no install yet),
            # or the two installed dirs aren't a genuine part pair (e.g. two
            # leftover version folders side by side): last-one-wins is
            # correct, matching pre-aggregation behavior.
            by_thread[tid] = LibraryMatch(
                **{**existing.__dict__, "install_path": d.install_path,
                   "installed_version": d.installed_version,
                   "disk_size_bytes": d.disk_size_bytes}
            )
        else:
            by_thread[tid] = LibraryMatch(
                f95_thread_id=tid, game_title=f95_by_id[tid].name,
                save_dir_name=save_name, last_played_at=None,
                first_played_at=None, save_count=None, total_save_size_bytes=None,
                install_path=d.install_path,
                installed_version=d.installed_version,
                disk_size_bytes=d.disk_size_bytes,
            )

    return MatchResult(
        matches=list(by_thread.values()),
        unmatched_installed=unmatched_disk,
        unmatched_play_history=unmatched_saves,
        learned_pairings=learned_pairings,
    )


def _resolve_save(
    save: PlayHistoryEntry,
    f95_rows: list[F95CheckerGame],
    f95_by_name: dict[str, int],
    cached_pairings: dict[str, int],
    fuzzy_threshold: int,
    corroboration_floor: int,
) -> tuple[int, str] | None:
    """Resolve a play-history entry to (f95_thread_id, method).

    Priority order:
    1. cached_pairings → ("cached",) — already persisted, highest confidence
    2. fuzzy WRatio >= fuzzy_threshold → ("name",)
    3. fuzzy WRatio >= corroboration_floor AND version intersection → ("version",)
    4. None — unmatched
    """
    key = save.save_dir_name

    # 1. Cached pairing always wins.
    if key in cached_pairings:
        return cached_pairings[key], "cached"

    if not f95_by_name:
        return None

    cleaned = _clean_for_match(key)
    best = process.extractOne(cleaned, list(f95_by_name.keys()), scorer=fuzz.WRatio)
    if best is None:
        return None

    candidate_name, score, _ = best

    # 2. High-confidence name match.
    if score >= fuzzy_threshold:
        return f95_by_name[candidate_name], "name"

    # 3. Version-corroborated match: fuzzy >= floor AND version tokens intersect.
    if score >= corroboration_floor:
        save_vtokens = version_tokens(save.last_played_version)
        if save_vtokens:
            # Look up the candidate F95 row to get its version.
            candidate_id = f95_by_name[candidate_name]
            # Build a quick id→game map from f95_rows (cheap, already in memory).
            f95_by_id = {f.id: f for f in f95_rows}
            candidate_game = f95_by_id.get(candidate_id)
            if candidate_game is not None:
                f95_vtokens = version_tokens(candidate_game.version)
                if save_vtokens & f95_vtokens:
                    return candidate_id, "version"

    return None


def _resolve(
    key: str,
    f95_by_name: dict[str, int],
    cached_pairings: dict[str, int],
    threshold: int,
) -> int | None:
    if key in cached_pairings:
        return cached_pairings[key]
    if not f95_by_name:
        return None
    cleaned = _clean_for_match(key)
    best = process.extractOne(cleaned, list(f95_by_name.keys()), scorer=fuzz.WRatio)
    if best is None:
        return None
    name, score, _ = best
    if score >= threshold:
        return f95_by_name[name]
    return None


def _resolve_disk(
    d: InstalledGame,
    f95_rows: list[F95CheckerGame],
    cached_pairings: dict[str, int],
    threshold: int,
) -> int | None:
    if d.folder_name in cached_pairings:
        return cached_pairings[d.folder_name]
    # Executable-path match
    install_str = str(d.install_path)
    for f in f95_rows:
        if f.executable and (
            f.executable.startswith(install_str) or install_str.startswith(str(Path(f.executable).parent))
        ):
            return f.id
    # Fuzzy fallback
    return _resolve(
        d.folder_name, {f.name: f.id for f in f95_rows}, cached_pairings, threshold
    )
