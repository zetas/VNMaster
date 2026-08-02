from __future__ import annotations

from pathlib import Path
import sqlite3

from vnmaster.browser_handoff import (
    capture_recent_provider_url,
    zen_history_available,
)


def _zen_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "zen"
    profile = root / "Profiles" / "test.default"
    profile.mkdir(parents=True)
    (root / "profiles.ini").write_text(
        "[InstallTEST]\nDefault=Profiles/test.default\nLocked=1\n",
        encoding="utf-8",
    )
    places = profile / "places.sqlite"
    with sqlite3.connect(places) as connection:
        connection.executescript(
            """
            CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT NOT NULL);
            CREATE TABLE moz_historyvisits (
                id INTEGER PRIMARY KEY,
                place_id INTEGER NOT NULL,
                visit_date INTEGER NOT NULL
            );
            """
        )
    return root, places


def test_capture_recent_provider_url_only_reads_matching_visits(tmp_path: Path) -> None:
    root, places = _zen_root(tmp_path)
    with sqlite3.connect(places) as connection:
        connection.executemany(
            "INSERT INTO moz_places (id, url) VALUES (?, ?)",
            (
                (1, "https://example.com/private"),
                (2, "https://gofile.io/d/old"),
                (3, "https://gofile.io/d/current"),
            ),
        )
        connection.executemany(
            "INSERT INTO moz_historyvisits (place_id, visit_date) VALUES (?, ?)",
            ((1, 300), (2, 100), (3, 250)),
        )

    assert zen_history_available(zen_root=root)
    assert (
        capture_recent_provider_url("GOFILE", since_us=200, zen_root=root)
        == "https://gofile.io/d/current"
    )


def test_capture_recent_provider_url_returns_none_without_new_match(tmp_path: Path) -> None:
    root, places = _zen_root(tmp_path)
    with sqlite3.connect(places) as connection:
        connection.execute(
            "INSERT INTO moz_places (id, url) VALUES (?, ?)",
            (1, "https://gofile.io/d/old"),
        )
        connection.execute(
            "INSERT INTO moz_historyvisits (place_id, visit_date) VALUES (?, ?)",
            (1, 100),
        )

    assert capture_recent_provider_url("GOFILE", since_us=200, zen_root=root) is None
