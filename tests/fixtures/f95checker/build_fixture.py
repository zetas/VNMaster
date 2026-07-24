"""Generates a tiny F95Checker-shaped SQLite for tests.

Run once; commit the resulting db_11x.sqlite3. Re-run if the F95Checker
schema changes.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "db_11x.sqlite3"


def build() -> None:
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE games (
            id INTEGER PRIMARY KEY,
            name TEXT,
            version TEXT,
            developer TEXT,
            engine TEXT,
            status TEXT,
            last_updated INTEGER,
            changelog TEXT,
            description TEXT,
            image_url TEXT,
            tags TEXT,
            executable TEXT,
            archived INTEGER,
            rating INTEGER
        )
        """
    )
    now = int(time.time())
    rows = [
        (
            42, "Eternum", "0.7.0", "Caribdis", "Ren'Py", "ongoing",
            now, "v0.7.0\n- 800 new renders", "Sci-fi VN", "img.png",
            "corruption,slow-burn", "/Users/x/Games/Eternum/MyGame.app", 0, 4,
        ),
        (
            99, "Summer's Gone", "0.10", "Oceanlab", "Ren'Py", "ongoing",
            now - 86400, "v0.10\n- minor", "Adult VN", "img2.png",
            "slow-burn,romance", None, 0, 5,
        ),
    ]
    cur.executemany(
        "INSERT INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    build()
