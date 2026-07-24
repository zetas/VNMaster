"""Selection step of the digest pipeline.

Pure read against vnmaster.db. Returns plain dataclasses for downstream
embed building.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Engine, select

from vnmaster.db.engine import session_scope
from vnmaster.db.models import LibraryGame
from vnmaster.magnitude import is_user_behind
from vnmaster.status import status_changed

if TYPE_CHECKING:
	from vnmaster.db.ro_f95checker import F95CheckerGame


@dataclass(frozen=True)
class SelectedUpdate:
    f95_thread_id: int
    game_title: str
    installed_version: str | None
    latest_upstream_version: str | None
    upstream_last_updated_at: int | None
    raw_changelog: str | None
    developer: str | None
    image_url: str | None
    upstream_thread_url: str | None
    last_played_at: int | None
    install_path: str | None
    tags_json: str | None
    status: str | None = None
    status_changed: bool = False


@dataclass(frozen=True)
class DigestCandidates:
    updates: list[SelectedUpdate]


def select_digest_candidates(
    *,
    engine: Engine,
    previous_digest_run_at: int,
    now_epoch: int,
    max_repeat_weeks: int,
    force_all_behind: bool = False,
) -> DigestCandidates:
    """Select library updates for a digest.

    `force_all_behind=True` ignores the "already shown recently" throttle
    and surfaces every tracked game whose played version differs from the
    latest upstream version. Used by `vnmaster digest --force` to produce
    an on-demand full status report rather than only what's new since the
    last run.
    """
    repeat_window = max_repeat_weeks * 7 * 86400
    updates: list[SelectedUpdate] = []

    with session_scope(engine) as s:
        for g in s.execute(select(LibraryGame)).scalars().all():
            if g.hidden:
                continue
            if (
                g.acknowledged_version is not None
                and g.acknowledged_version == g.latest_upstream_version
            ):
                continue

            fresh_update = (
                g.upstream_last_updated_at is not None
                and g.upstream_last_updated_at > previous_digest_run_at
            )
            user_behind = (
                is_user_behind(g.installed_version, g.latest_upstream_version)
                and g.last_seen_in_digest_at is not None
                and now_epoch - g.last_seen_in_digest_at > repeat_window
            )
            # Date-based fallback: when we don't know the played version
            # (no version in the save and the game isn't installed on disk),
            # treat "upstream updated after you last played" as a likely
            # update. Throttled by the same repeat window so it doesn't
            # re-surface every week.
            played_before_update = (
                g.installed_version is None
                and g.last_played_at is not None
                and g.upstream_last_updated_at is not None
                and g.upstream_last_updated_at > g.last_played_at
                and (
                    g.last_seen_in_digest_at is None
                    or now_epoch - g.last_seen_in_digest_at > repeat_window
                )
            )
            # --force: surface any game where the played version differs
            # from upstream, ignoring the recently-shown throttle.
            forced = (
                force_all_behind
                and (
                    is_user_behind(g.installed_version, g.latest_upstream_version)
                    or (g.installed_version is None
                        and g.last_played_at is not None
                        and g.upstream_last_updated_at is not None
                        and g.upstream_last_updated_at > g.last_played_at)
                )
            )
            if not (fresh_update or user_behind or played_before_update or forced):
                continue

            updates.append(
                SelectedUpdate(
                    f95_thread_id=g.f95_thread_id,
                    game_title=g.game_title,
                    installed_version=g.installed_version,
                    latest_upstream_version=g.latest_upstream_version,
                    upstream_last_updated_at=g.upstream_last_updated_at,
                    raw_changelog=g.raw_changelog,
                    developer=g.developer,
                    image_url=g.image_url,
                    upstream_thread_url=g.upstream_thread_url,
                    last_played_at=g.last_played_at,
                    install_path=g.install_path,
                    tags_json=g.tags_json,
                    status=g.status,
                    status_changed=bool(g.status_changed),
                )
            )

    return DigestCandidates(updates=updates)


def select_daily_candidates(
    *,
    engine: Engine,
    f95_rows: list[F95CheckerGame],
    now_epoch: int,
) -> DigestCandidates:
    """Select games for a nightly alert.

    Fires the first time a tracked game reaches a new upstream version the user
    is behind on, or the first time its notable status changes — each deduped
    against a per-game watermark (`last_daily_notified_version` /
    `last_daily_notified_status`) so a given change alerts once, not every night.

    Live upstream version/status/changelog come from `f95_rows` so this never
    depends on daily-mode upserts writing status.
    """
    by_id = {f.id: f for f in f95_rows}
    updates: list[SelectedUpdate] = []

    with session_scope(engine) as s:
        for g in s.execute(select(LibraryGame)).scalars().all():
            if g.hidden:
                continue
            f = by_id.get(g.f95_thread_id)
            if f is None:
                continue
            live_version = f.version
            live_status = f.status
            if (
                g.acknowledged_version is not None
                and g.acknowledged_version == live_version
            ):
                continue

            version_signal = (
                live_version is not None
                and is_user_behind(g.installed_version, live_version)
                and g.last_daily_notified_version != live_version
            )
            status_signal = status_changed(g.last_daily_notified_status, live_status)
            if not (version_signal or status_signal):
                continue

            updates.append(
                SelectedUpdate(
                    f95_thread_id=g.f95_thread_id,
                    game_title=g.game_title,
                    installed_version=g.installed_version,
                    latest_upstream_version=live_version,
                    upstream_last_updated_at=f.last_updated,
                    raw_changelog=f.changelog,
                    developer=f.developer,
                    image_url=f.image_url,
                    upstream_thread_url=(
                        f"https://f95zone.to/threads/.{g.f95_thread_id}/"
                    ),
                    last_played_at=g.last_played_at,
                    install_path=g.install_path,
                    tags_json=json.dumps(f.tags) if f.tags is not None else g.tags_json,
                    status=live_status,
                    status_changed=status_signal,
                )
            )

    return DigestCandidates(updates=updates)
