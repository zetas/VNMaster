"""Pure state-transition function for reactions.

The bot's event handler resolves (message_id, emoji) → (thread_id, kind, action)
and then calls this function. Separated so the state machine can be unit-tested
without discord.py.
"""
from __future__ import annotations

from enum import Enum

from sqlalchemy import Engine, select

from vnmaster.db.engine import session_scope
from vnmaster.db.models import LibraryGame


class ReactionAction(str, Enum):
    INTERESTED = "interested"
    ACKNOWLEDGED = "acknowledged"  # update only — "📥 already grabbed"
    HIDE = "hide"  # kept for update reactions (hide a game from future digests)


def apply_reaction(
    *,
    engine: Engine,
    thread_id: int,
    kind: str,
    action: ReactionAction,
    now_epoch: int,
    skip_window_weeks: int = 4,
) -> None:
    if kind == "update":
        _apply_update_reaction(
            engine=engine, thread_id=thread_id, action=action, now_epoch=now_epoch
        )
    else:
        raise ValueError(f"unknown reaction kind: {kind!r}")


def _apply_update_reaction(
    *, engine: Engine, thread_id: int, action: ReactionAction, now_epoch: int
) -> None:
    with session_scope(engine) as s:
        g = s.execute(
            select(LibraryGame).where(LibraryGame.f95_thread_id == thread_id)
        ).scalar_one_or_none()
        if g is None:
            return
        if action is ReactionAction.HIDE:
            g.hidden = 1
        elif action is ReactionAction.INTERESTED:
            g.interested = 1
        elif action is ReactionAction.ACKNOWLEDGED:
            g.acknowledged_version = g.latest_upstream_version
        else:
            raise ValueError(f"action {action} invalid for kind='update'")
        g.updated_at = now_epoch
