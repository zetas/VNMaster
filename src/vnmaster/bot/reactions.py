"""Reaction emoji → action resolver, message_id → thread lookup router,
and interest-link message builder.
"""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import Engine, select

from vnmaster.bot.apply_reaction import ReactionAction, apply_reaction
from vnmaster.db.engine import session_scope
from vnmaster.db.models import DigestEntry, LibraryGame
from vnmaster.logging_setup import get_logger

log = get_logger(__name__)

# U+FE0F is the "variation selector-16" (VS16) that Discord sometimes appends
# (or omits) on emoji.  We normalise by stripping it before map lookup, and
# store map keys without it so both forms resolve identically.
_VS16 = "️"


def _norm(emoji: str) -> str:
    """Strip all VS16 variation-selector codepoints from *emoji*."""
    return emoji.replace(_VS16, "")


# Keys stored WITHOUT VS16 so _norm(incoming) always matches.
_UPDATE_MAP: dict[str, ReactionAction] = {
    _norm("⬇️"): ReactionAction.INTERESTED,   # U+2B07 — reply in-channel w/ link
    _norm("📦"): ReactionAction.ACKNOWLEDGED,  # U+1F4E6 — mark version grabbed
}


@dataclass(frozen=True)
class RoutedReaction:
    """Returned by :func:`route_reaction` when a reaction was successfully applied."""

    kind: str
    thread_id: int
    action: ReactionAction


def resolve_reaction(kind: str, emoji: str) -> ReactionAction | None:
    normalised = _norm(emoji)
    if kind == "update":
        return _UPDATE_MAP.get(normalised)
    return None


def route_reaction(
    *,
    engine: Engine,
    message_id: str,
    emoji: str,
    now_epoch: int,
    skip_window_weeks: int = 4,
) -> RoutedReaction | None:
    with session_scope(engine) as s:
        entry = s.execute(
            select(DigestEntry).where(DigestEntry.discord_message_id == message_id)
        ).scalar_one_or_none()
        if entry is None:
            log.debug("reaction on unknown message %s", message_id)
            return None
        kind = entry.kind
        thread_id = entry.f95_thread_id

    action = resolve_reaction(kind, emoji)
    if action is None:
        log.debug("ignoring reaction %s on %s message", emoji, kind)
        return None

    apply_reaction(
        engine=engine,
        thread_id=thread_id,
        kind=kind,
        action=action,
        now_epoch=now_epoch,
        skip_window_weeks=skip_window_weeks,
    )
    return RoutedReaction(kind=kind, thread_id=thread_id, action=action)


def build_interest_message(*, engine: Engine, thread_id: int) -> str | None:
    """In-channel reply text for an INTERESTED (⬇️) reaction on an update:
    the game title + its F95 thread URL.  Returns None if the row is missing.
    """
    with session_scope(engine) as s:
        game = s.execute(
            select(LibraryGame).where(LibraryGame.f95_thread_id == thread_id)
        ).scalar_one_or_none()
        if game is None:
            return None
        title = game.game_title
        url = game.upstream_thread_url

    if url:
        return f"**{title}** — grab it here: {url}"
    return f"**{title}** — open thread #{thread_id} in F95Checker."
