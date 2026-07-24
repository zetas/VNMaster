"""Tests for build_interest_message and the client in-channel posting on INTERESTED."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vnmaster.bot.reactions import build_interest_message
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, LibraryGame


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine(tmp_path: Path):
    e = create_engine_for(tmp_path / "v.db")
    Base.metadata.create_all(e)
    return e


def _seed_game(
    engine,
    *,
    thread_id: int = 42,
    game_title: str = "Eternum",
    upstream_thread_url: str | None = "https://f95zone.to/threads/eternum.12345/",
) -> None:
    with session_scope(engine) as s:
        s.add(LibraryGame(
            f95_thread_id=thread_id,
            game_title=game_title,
            latest_upstream_version="1.0",
            created_at=1,
            updated_at=1,
            upstream_thread_url=upstream_thread_url,
        ))


# ---------------------------------------------------------------------------
# build_interest_message unit tests (same behavior as old build_interest_dm)
# ---------------------------------------------------------------------------

def test_build_interest_message_returns_title_and_url(tmp_path: Path) -> None:
    """Returns a string with the game title and its F95 thread URL."""
    engine = _engine(tmp_path)
    _seed_game(engine, thread_id=42, game_title="Eternum",
               upstream_thread_url="https://f95zone.to/threads/eternum.12345/")
    result = build_interest_message(engine=engine, thread_id=42)
    assert result is not None
    assert "Eternum" in result
    assert "https://f95zone.to/threads/eternum.12345/" in result


def test_build_interest_message_fallback_when_url_is_none(tmp_path: Path) -> None:
    """Falls back to thread-id text when upstream_thread_url is None."""
    engine = _engine(tmp_path)
    _seed_game(engine, thread_id=42, game_title="Eternum", upstream_thread_url=None)
    result = build_interest_message(engine=engine, thread_id=42)
    assert result is not None
    assert "Eternum" in result
    assert "42" in result  # thread id in fallback


def test_build_interest_message_returns_none_when_row_absent(tmp_path: Path) -> None:
    """Returns None if the LibraryGame row doesn't exist."""
    engine = _engine(tmp_path)
    result = build_interest_message(engine=engine, thread_id=9999)
    assert result is None


# ---------------------------------------------------------------------------
# Client in-channel wiring — INTERESTED now posts to channel, not DM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_raw_reaction_add_posts_to_channel_on_interested(tmp_path: Path) -> None:
    """When ⬇️ fires on an update message, the handler posts to the configured
    channel (with a ping + reply reference) instead of DMing the user."""
    from vnmaster.bot.apply_reaction import ReactionAction
    from vnmaster.bot.reactions import RoutedReaction
    import discord

    routed = RoutedReaction(kind="update", thread_id=42, action=ReactionAction.INTERESTED)
    msg_text = "**Eternum** — grab it here: https://f95zone.to/threads/eternum.12345/"

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    with (
        patch("vnmaster.bot.client.route_reaction", return_value=routed),
        patch("vnmaster.bot.client.build_interest_message", return_value=msg_text),
    ):
        from sqlalchemy import create_engine
        from vnmaster.bot.client import VNMasterBot
        from vnmaster.db.models import Base as _Base

        engine = create_engine(f"sqlite:///{tmp_path / 'v.db'}", future=True)
        _Base.metadata.create_all(engine)
        cfg = tmp_path / "config.toml"
        cfg.write_text("[discovery]\ninclude_tags = []\nexclude_tags = []\n")
        bot = VNMasterBot(
            engine=engine, channel_id=1001, guild_id=123, config_path=cfg
        )
        bot._connection = MagicMock()  # type: ignore[attr-defined]
        # get_channel returns our mock channel
        bot.get_channel = MagicMock(return_value=mock_channel)

        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.channel_id = 1001
        payload.user_id = 999
        payload.message_id = 111
        payload.emoji = MagicMock()
        payload.emoji.__str__ = MagicMock(return_value="⬇️")

        with patch.object(type(bot), "user", new_callable=lambda: property(lambda self: MagicMock(id=1))):
            await bot.on_raw_reaction_add(payload)

    # channel.send must have been called with the user ping + text + reference
    mock_channel.send.assert_awaited_once()
    call_args, call_kwargs = mock_channel.send.call_args
    sent_text = call_args[0] if call_args else call_kwargs.get("content", "")
    assert "<@999>" in sent_text
    assert msg_text in sent_text
    assert "reference" in call_kwargs
    assert "allowed_mentions" in call_kwargs


@pytest.mark.asyncio
async def test_on_raw_reaction_add_no_channel_post_on_acknowledged(tmp_path: Path) -> None:
    """📦 (ACKNOWLEDGED) never triggers a channel post."""
    from vnmaster.bot.apply_reaction import ReactionAction
    from vnmaster.bot.reactions import RoutedReaction
    import discord

    routed = RoutedReaction(kind="update", thread_id=42, action=ReactionAction.ACKNOWLEDGED)

    with (
        patch("vnmaster.bot.client.route_reaction", return_value=routed),
        patch("vnmaster.bot.client.build_interest_message") as mock_msg,
    ):
        from sqlalchemy import create_engine
        from vnmaster.bot.client import VNMasterBot
        from vnmaster.db.models import Base as _Base

        engine = create_engine(f"sqlite:///{tmp_path / 'v.db'}", future=True)
        _Base.metadata.create_all(engine)
        cfg = tmp_path / "config.toml"
        cfg.write_text("[discovery]\ninclude_tags = []\nexclude_tags = []\n")
        bot = VNMasterBot(
            engine=engine, channel_id=1001, guild_id=123, config_path=cfg
        )
        bot.get_channel = MagicMock()

        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.channel_id = 1001
        payload.user_id = 999
        payload.message_id = 111
        payload.emoji = MagicMock()
        payload.emoji.__str__ = MagicMock(return_value="📦")

        with patch.object(type(bot), "user", new_callable=lambda: property(lambda self: MagicMock(id=1))):
            await bot.on_raw_reaction_add(payload)

    mock_msg.assert_not_called()
    bot.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_on_raw_reaction_add_channel_send_failure_does_not_crash(tmp_path: Path) -> None:
    """If channel.send raises, the handler swallows the error (no crash)."""
    from vnmaster.bot.apply_reaction import ReactionAction
    from vnmaster.bot.reactions import RoutedReaction
    import discord

    routed = RoutedReaction(kind="update", thread_id=42, action=ReactionAction.INTERESTED)
    msg_text = "**Eternum** — grab it here: https://f95zone.to/threads/eternum.12345/"

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(side_effect=Exception("Cannot post"))

    with (
        patch("vnmaster.bot.client.route_reaction", return_value=routed),
        patch("vnmaster.bot.client.build_interest_message", return_value=msg_text),
    ):
        from sqlalchemy import create_engine
        from vnmaster.bot.client import VNMasterBot
        from vnmaster.db.models import Base as _Base

        engine = create_engine(f"sqlite:///{tmp_path / 'v.db'}", future=True)
        _Base.metadata.create_all(engine)
        cfg = tmp_path / "config.toml"
        cfg.write_text("[discovery]\ninclude_tags = []\nexclude_tags = []\n")
        bot = VNMasterBot(
            engine=engine, channel_id=1001, guild_id=123, config_path=cfg
        )
        bot.get_channel = MagicMock(return_value=mock_channel)

        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.channel_id = 1001
        payload.user_id = 999
        payload.message_id = 111
        payload.emoji = MagicMock()
        payload.emoji.__str__ = MagicMock(return_value="⬇️")

        with patch.object(type(bot), "user", new_callable=lambda: property(lambda self: MagicMock(id=1))):
            # Must not raise
            await bot.on_raw_reaction_add(payload)


@pytest.mark.asyncio
async def test_on_raw_reaction_add_falls_back_to_fetch_channel(tmp_path: Path) -> None:
    """When get_channel returns None, fetch_channel is awaited as fallback."""
    from vnmaster.bot.apply_reaction import ReactionAction
    from vnmaster.bot.reactions import RoutedReaction
    import discord

    routed = RoutedReaction(kind="update", thread_id=42, action=ReactionAction.INTERESTED)
    msg_text = "**Eternum** — grab it here: https://f95zone.to/threads/eternum.12345/"

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    with (
        patch("vnmaster.bot.client.route_reaction", return_value=routed),
        patch("vnmaster.bot.client.build_interest_message", return_value=msg_text),
    ):
        from sqlalchemy import create_engine
        from vnmaster.bot.client import VNMasterBot
        from vnmaster.db.models import Base as _Base

        engine = create_engine(f"sqlite:///{tmp_path / 'v.db'}", future=True)
        _Base.metadata.create_all(engine)
        cfg = tmp_path / "config.toml"
        cfg.write_text("[discovery]\ninclude_tags = []\nexclude_tags = []\n")
        bot = VNMasterBot(
            engine=engine, channel_id=1001, guild_id=123, config_path=cfg
        )
        # get_channel returns None → triggers fetch_channel fallback
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(return_value=mock_channel)

        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.channel_id = 1001
        payload.user_id = 999
        payload.message_id = 111
        payload.emoji = MagicMock()
        payload.emoji.__str__ = MagicMock(return_value="⬇️")

        with patch.object(type(bot), "user", new_callable=lambda: property(lambda self: MagicMock(id=1))):
            await bot.on_raw_reaction_add(payload)

    bot.fetch_channel.assert_awaited_once_with(1001)
    mock_channel.send.assert_awaited_once()
