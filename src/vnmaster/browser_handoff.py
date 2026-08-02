"""Capture a provider URL from a user-approved Zen browser handoff."""

from __future__ import annotations

import configparser
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from vnmaster.downloads.downloader import is_url_for_host


class BrowserHandoffError(RuntimeError):
    pass


_PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "mega": ("mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"),
    "gofile": ("gofile.io", "www.gofile.io"),
    "pixeldrain": ("pixeldrain.com", "www.pixeldrain.com"),
    "datanodes": ("datanodes.to", "www.datanodes.to"),
    "vikingfile": ("vikingfile.com", "www.vikingfile.com"),
    "mediafire": ("mediafire.com", "www.mediafire.com"),
}


def zen_history_available(*, zen_root: Path | None = None) -> bool:
    """Return whether VNMaster can locate Zen's active history database."""
    return _zen_places_path(zen_root=zen_root) is not None


def capture_recent_provider_url(
    provider: str,
    *,
    since_us: int,
    zen_root: Path | None = None,
) -> str | None:
    """Return a matching Zen history URL visited after ``since_us``.

    Only exact HTTPS host patterns for the requested provider are queried. The
    browser database is copied into a private temporary directory so Zen keeps
    ownership of its live SQLite/WAL files and no browser cookies are accessed.
    """
    places_path = _zen_places_path(zen_root=zen_root)
    if places_path is None:
        raise BrowserHandoffError("Zen's active history database was not found")
    provider_key = re.sub(r"[^a-z0-9]", "", provider.casefold())
    hosts = _PROVIDER_HOSTS.get(provider_key)
    if hosts is None:
        raise BrowserHandoffError(
            f"Automatic Zen capture is not supported for {provider!r}"
        )
    if since_us < 0:
        raise BrowserHandoffError("Browser handoff start time is invalid")

    last_error: OSError | sqlite3.Error | None = None
    for _attempt in range(3):
        try:
            urls = _query_history_snapshot(places_path, hosts, since_us)
        except (OSError, sqlite3.Error) as exc:
            last_error = exc
            continue
        return next((url for url in urls if is_url_for_host(provider, url)), None)
    detail = f": {last_error}" if last_error is not None else ""
    raise BrowserHandoffError(f"Could not read Zen's recent history{detail}")


def _query_history_snapshot(
    places_path: Path,
    hosts: tuple[str, ...],
    since_us: int,
) -> tuple[str, ...]:
    with tempfile.TemporaryDirectory(prefix="vnmaster-zen-history-") as temp_name:
        snapshot = Path(temp_name) / "places.sqlite"
        shutil.copy2(places_path, snapshot)
        for suffix in ("-wal", "-shm"):
            source = places_path.with_name(places_path.name + suffix)
            if source.is_file():
                shutil.copy2(source, snapshot.with_name(snapshot.name + suffix))

        patterns = tuple(f"https://{host}/%" for host in hosts)
        padded_patterns = (*patterns, *("",) * (4 - len(patterns)))
        query = """
            SELECT p.url, MAX(v.visit_date) AS latest_visit
            FROM moz_places AS p
            JOIN moz_historyvisits AS v ON v.place_id = p.id
            WHERE v.visit_date >= ?
              AND (
                  p.url LIKE ? OR p.url LIKE ? OR p.url LIKE ? OR p.url LIKE ?
              )
            GROUP BY p.url
            ORDER BY latest_visit DESC
            LIMIT 20
        """
        with sqlite3.connect(snapshot) as connection:
            rows = connection.execute(query, (since_us, *padded_patterns)).fetchall()
    return tuple(str(row[0]) for row in rows if isinstance(row[0], str))


def _zen_places_path(*, zen_root: Path | None = None) -> Path | None:
    root = (
        zen_root
        if zen_root is not None
        else Path.home() / "Library" / "Application Support" / "zen"
    )
    profiles_path = root / "profiles.ini"
    if not profiles_path.is_file():
        return None

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(profiles_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None

    sections = [section for section in parser.sections() if section.startswith("Install")]
    sections.extend(
        section
        for section in parser.sections()
        if section.startswith("Profile") and parser.getboolean(section, "Default", fallback=False)
    )
    sections.extend(section for section in parser.sections() if section.startswith("Profile"))

    seen: set[Path] = set()
    for section in sections:
        raw_path = (
            parser.get(section, "Default", fallback="")
            if section.startswith("Install")
            else parser.get(section, "Path", fallback="")
        )
        if not raw_path:
            continue
        relative = section.startswith("Install") or parser.getboolean(
            section, "IsRelative", fallback=True
        )
        candidate = (root / raw_path) if relative else Path(raw_path)
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        places = candidate / "places.sqlite"
        if places.is_file():
            return places
    return None
