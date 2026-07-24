"""Slash command handlers (pure functions).

The discord.py command shells live elsewhere; these are the unit-testable
business logic.
"""
from __future__ import annotations

import re

from sqlalchemy import Engine, func, select

from vnmaster.db.engine import session_scope
from vnmaster.db.models import DigestRun, LibraryGame, Pairing


_F95_THREAD_RE = re.compile(r"f95zone\.to/threads/[^/]*\.(\d+)/?")


class InvalidUrlError(ValueError):
    pass


class NoSuchPairingError(ValueError):
    pass


def cmd_pair(*, engine: Engine, name: str, f95_url: str, now_epoch: int) -> str:
    m = _F95_THREAD_RE.search(f95_url)
    if not m:
        raise InvalidUrlError(f"{f95_url!r} does not look like an F95 thread URL")
    thread_id = int(m.group(1))
    with session_scope(engine) as s:
        s.merge(Pairing(
            f95_thread_id=thread_id,
            save_dir_name=name,
            confidence=1.0,
            paired_at=now_epoch,
        ))
    return f"Paired {name!r} → thread #{thread_id}"


def cmd_pairings_list(*, engine: Engine) -> str:
    with session_scope(engine) as s:
        rows = list(
            s.execute(
                select(Pairing).order_by(Pairing.f95_thread_id)
            ).scalars()
        )
        # Build a thread_id → game_title lookup from LibraryGame.
        thread_ids = [r.f95_thread_id for r in rows]
        title_map: dict[int, str] = {}
        if thread_ids:
            games = s.execute(
                select(LibraryGame).where(LibraryGame.f95_thread_id.in_(thread_ids))
            ).scalars()
            for g in games:
                title_map[g.f95_thread_id] = g.game_title

    if not rows:
        return "No pairings yet."

    lines = [f"Pairings ({len(rows)} total):"]
    for p in rows:
        title = title_map.get(p.f95_thread_id, "(unknown)")
        names: list[str] = []
        if p.save_dir_name:
            names.append(f"save_dir={p.save_dir_name!r}")
        if p.folder_name:
            names.append(f"folder={p.folder_name!r}")
        name_part = ", ".join(names) if names else "(no name)"
        lines.append(
            f"  #{p.f95_thread_id}  {name_part}  conf={p.confidence:.2f}  title={title!r}"
        )
    return "\n".join(lines)


def cmd_unpair(*, engine: Engine, name: str) -> str:
    with session_scope(engine) as s:
        if name.isdigit():
            matches = list(
                s.execute(
                    select(Pairing).where(Pairing.f95_thread_id == int(name))
                ).scalars()
            )
        else:
            matches = list(
                s.execute(
                    select(Pairing).where(
                        (Pairing.save_dir_name == name) | (Pairing.folder_name == name)
                    )
                ).scalars()
            )
        if not matches:
            raise NoSuchPairingError(name)
        thread_ids = [p.f95_thread_id for p in matches]
        for p in matches:
            s.delete(p)

    if len(thread_ids) == 1:
        return f"Removed pairing: thread #{thread_ids[0]} ({name!r})"
    ids_str = ", ".join(f"#{tid}" for tid in thread_ids)
    return f"Removed {len(thread_ids)} pairings: {ids_str} (matched {name!r})"


def cmd_status(*, engine: Engine) -> str:
    with session_scope(engine) as s:
        last = s.execute(
            select(DigestRun).order_by(DigestRun.run_at.desc()).limit(1)
        ).scalar_one_or_none()
        library_count = s.execute(
            select(func.count()).select_from(LibraryGame)
        ).scalar_one()
        unmatched = s.execute(
            select(func.count())
            .select_from(LibraryGame)
            .where(LibraryGame.latest_upstream_version.is_(None))
        ).scalar_one()
    if last is None:
        return f"No digest runs yet. Library size: {library_count}. Unmatched: {unmatched}."
    return (
        f"Last digest run: {last.run_at} epoch · "
        f"updates: {last.updates_count} · "
        f"llm spend: ${last.llm_cost_usd:.2f}. "
        f"Library size: {library_count}. Unmatched (no upstream version): {unmatched}."
    )
