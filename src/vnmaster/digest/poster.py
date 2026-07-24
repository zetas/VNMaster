"""Posts a digest to Discord and stamps reactions.

Posting goes through a webhook (no auth, no rate limit headaches), but
reactions require a bot-authenticated client because webhooks can't react.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


UPDATE_REACTIONS = ("⬇️", "📦")


class Webhook(Protocol):
    async def send(
        self, content: str | None = ..., embeds: list[Any] | None = ...
    ) -> Any: ...


class BotClient(Protocol):
    async def add_reaction(
        self, channel_id: str, message_id: str, emoji: str
    ) -> None: ...


@dataclass(frozen=True)
class PostedDigest:
    # tuples of (message_id, embed_index, kind, f95_thread_id)
    entries: list[tuple[str, int, str, int]] = field(default_factory=list)


class DiscordPoster:
    def __init__(
        self, *, webhook: Webhook, bot_client: BotClient, channel_id: str
    ) -> None:
        self._webhook = webhook
        self._bot = bot_client
        self._channel_id = channel_id

    async def post(
        self,
        *,
        kickoff_text: str,
        update_embeds: list[tuple[int, dict[str, Any]]],
    ) -> PostedDigest:
        await self._webhook.send(content=kickoff_text)

        entries: list[tuple[str, int, str, int]] = []
        for thread_id, embed in update_embeds:
            msg = await self._webhook.send(embeds=[embed])
            entries.append((msg.id, 0, "update", thread_id))
            for emoji in UPDATE_REACTIONS:
                await self._bot.add_reaction(self._channel_id, msg.id, emoji)

        return PostedDigest(entries=entries)
