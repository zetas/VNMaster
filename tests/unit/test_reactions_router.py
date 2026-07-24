from pathlib import Path

import pytest
from sqlalchemy import select

from vnmaster.bot.apply_reaction import ReactionAction
from vnmaster.bot.reactions import RoutedReaction, resolve_reaction, route_reaction
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, DigestEntry, DigestRun, LibraryGame


# ---------------------------------------------------------------------------
# resolve_reaction — update emoji set
# ---------------------------------------------------------------------------

def test_resolve_new_update_emojis() -> None:
    """⬇️ → INTERESTED, 📦 → ACKNOWLEDGED."""
    assert resolve_reaction("update", "⬇️") is ReactionAction.INTERESTED
    assert resolve_reaction("update", "📦") is ReactionAction.ACKNOWLEDGED


def test_resolve_old_update_emojis_removed() -> None:
    """Old update emoji no longer map to any action."""
    assert resolve_reaction("update", "♻️") is None
    assert resolve_reaction("update", "📥") is None


def test_resolve_hide_still_absent_from_update_map() -> None:
    """❌ is not in the update map."""
    assert resolve_reaction("update", "❌") is None


def test_resolve_unknown_emoji_returns_none() -> None:
    assert resolve_reaction("update", "🍆") is None


def test_resolve_unknown_kind_returns_none() -> None:
    """An unknown kind always returns None."""
    assert resolve_reaction("discovery", "✅") is None
    assert resolve_reaction("garbage", "⬇️") is None


# ---------------------------------------------------------------------------
# VS16 normalisation — ⬇️ (with selector) and ⬇ (without) both work
# ---------------------------------------------------------------------------

def test_resolve_down_arrow_with_vs16() -> None:
    """⬇️ (U+2B07 U+FE0F) resolves to INTERESTED."""
    assert resolve_reaction("update", "⬇️") is ReactionAction.INTERESTED


def test_resolve_down_arrow_without_vs16() -> None:
    """⬇ (U+2B07 alone, no variation selector) resolves to INTERESTED."""
    assert resolve_reaction("update", "⬇") is ReactionAction.INTERESTED


def test_resolve_package_with_vs16() -> None:
    """📦 resolves to ACKNOWLEDGED regardless of variation selector."""
    assert resolve_reaction("update", "📦") is ReactionAction.ACKNOWLEDGED
    assert resolve_reaction("update", "📦️") is ReactionAction.ACKNOWLEDGED


# ---------------------------------------------------------------------------
# RoutedReaction dataclass
# ---------------------------------------------------------------------------

def test_routed_reaction_is_frozen_dataclass() -> None:
    rr = RoutedReaction(kind="update", thread_id=42, action=ReactionAction.INTERESTED)
    assert rr.kind == "update"
    assert rr.thread_id == 42
    assert rr.action is ReactionAction.INTERESTED
    # frozen — should raise on assignment
    with pytest.raises((AttributeError, TypeError)):
        rr.kind = "discovery"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# route_reaction — now returns RoutedReaction | None
# ---------------------------------------------------------------------------

def _engine(tmp_path: Path):
    e = create_engine_for(tmp_path / "v.db")
    Base.metadata.create_all(e)
    return e


def _seed(
    engine,
    message_id: str,
    kind: str,
    thread_id: int,
    upstream_thread_url: str | None = None,
) -> None:
    with session_scope(engine) as s:
        run = DigestRun(
            run_at=1, updates_count=0, llm_calls=0, llm_cost_usd=0.0
        )
        s.add(run)
        s.flush()
        s.add(DigestEntry(
            run_id=run.id, discord_message_id=message_id, embed_index=0,
            kind=kind, f95_thread_id=thread_id,
        ))
        s.add(LibraryGame(
            f95_thread_id=thread_id, game_title="X",
            latest_upstream_version="0.1", created_at=1, updated_at=1,
            upstream_thread_url=upstream_thread_url,
        ))


def test_route_down_arrow_returns_interested_routed_reaction(tmp_path: Path) -> None:
    """⬇️ on an update entry → RoutedReaction(kind='update', …, INTERESTED)."""
    engine = _engine(tmp_path)
    _seed(engine, message_id="m-1", kind="update", thread_id=42)
    result = route_reaction(engine=engine, message_id="m-1", emoji="⬇️", now_epoch=100)
    assert result is not None
    assert result.kind == "update"
    assert result.thread_id == 42
    assert result.action is ReactionAction.INTERESTED


def test_route_package_returns_acknowledged_routed_reaction(tmp_path: Path) -> None:
    """📦 on an update entry → RoutedReaction(…, ACKNOWLEDGED); game row updated."""
    engine = _engine(tmp_path)
    _seed(engine, message_id="m-1", kind="update", thread_id=42)
    result = route_reaction(engine=engine, message_id="m-1", emoji="📦", now_epoch=100)
    assert result is not None
    assert result.action is ReactionAction.ACKNOWLEDGED
    with session_scope(engine) as s:
        g = s.execute(select(LibraryGame)).scalar_one()
        assert g.acknowledged_version == "0.1"


def test_route_cross_on_update_returns_none(tmp_path: Path) -> None:
    """❌ is not valid on update messages — returns None, applies nothing."""
    engine = _engine(tmp_path)
    _seed(engine, message_id="m-1", kind="update", thread_id=42)
    result = route_reaction(engine=engine, message_id="m-1", emoji="❌", now_epoch=100)
    assert result is None
    with session_scope(engine) as s:
        g = s.execute(select(LibraryGame)).scalar_one()
        assert g.hidden == 0


def test_route_returns_none_for_unknown_message(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    result = route_reaction(engine=engine, message_id="unknown", emoji="❌", now_epoch=100)
    assert result is None


def test_route_returns_none_for_unrecognized_emoji(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine, message_id="m-1", kind="update", thread_id=42)
    result = route_reaction(engine=engine, message_id="m-1", emoji="🍆", now_epoch=100)
    assert result is None
    with session_scope(engine) as s:
        g = s.execute(select(LibraryGame)).scalar_one()
        assert g.hidden == 0
