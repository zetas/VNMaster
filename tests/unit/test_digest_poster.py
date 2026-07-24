from unittest.mock import AsyncMock, MagicMock

import pytest

from vnmaster.digest.poster import UPDATE_REACTIONS, DiscordPoster, PostedDigest


@pytest.mark.asyncio
async def test_post_sends_kickoff_then_embeds() -> None:
    webhook = AsyncMock()
    webhook.send.return_value = MagicMock(id="msg123")
    bot_client = MagicMock()
    bot_client.add_reaction = AsyncMock()
    bot_client.fetch_channel = AsyncMock()

    poster = DiscordPoster(
        webhook=webhook,
        bot_client=bot_client,
        channel_id="channel-1",
    )
    update_embed = {"title": "U", "color": 0}
    result = await poster.post(
        kickoff_text="Weekly",
        update_embeds=[(42, update_embed)],
    )
    assert isinstance(result, PostedDigest)
    # Kickoff + one update
    assert webhook.send.await_count == 2
    # Reactions: 2 on update
    assert bot_client.add_reaction.await_count == 2


@pytest.mark.asyncio
async def test_each_message_carries_one_embed() -> None:
    webhook = AsyncMock()
    webhook.send.return_value = MagicMock(id="m")
    bot_client = MagicMock()
    bot_client.add_reaction = AsyncMock()
    poster = DiscordPoster(webhook=webhook, bot_client=bot_client, channel_id="c")
    await poster.post(
        kickoff_text="W",
        update_embeds=[(1, {}), (2, {}), (3, {})],
    )
    # 1 kickoff + 3 updates = 4 calls. Each update call has 1 embed.
    update_calls = webhook.send.call_args_list[1:]
    for call_item in update_calls:
        assert len(call_item.kwargs["embeds"]) == 1


@pytest.mark.asyncio
async def test_post_records_message_ids_per_thread() -> None:
    webhook = AsyncMock()
    webhook.send.side_effect = [
        MagicMock(id="k"),
        MagicMock(id="u-1"),
    ]
    bot_client = MagicMock()
    bot_client.add_reaction = AsyncMock()
    poster = DiscordPoster(webhook=webhook, bot_client=bot_client, channel_id="c")
    result = await poster.post(
        kickoff_text="W",
        update_embeds=[(42, {})],
    )
    assert result.entries == [
        ("u-1", 0, "update", 42),
    ]


def test_update_reactions_are_down_arrow_and_package() -> None:
    """Update reactions must be exactly ⬇️ then 📦, no more."""
    assert UPDATE_REACTIONS == ("⬇️", "📦")
    assert "♻️" not in UPDATE_REACTIONS
    assert "❌" not in UPDATE_REACTIONS
    assert "📥" not in UPDATE_REACTIONS


@pytest.mark.asyncio
async def test_update_embed_gets_down_arrow_then_package() -> None:
    """Update embeds receive ⬇️ then 📦 — in that order."""
    webhook = AsyncMock()
    webhook.send.return_value = MagicMock(id="m1")
    bot_client = MagicMock()
    bot_client.add_reaction = AsyncMock()
    poster = DiscordPoster(webhook=webhook, bot_client=bot_client, channel_id="c")

    await poster.post(
        kickoff_text="W",
        update_embeds=[(42, {})],
    )

    emoji_calls = [c.args[2] for c in bot_client.add_reaction.call_args_list]
    assert emoji_calls == ["⬇️", "📦"]
