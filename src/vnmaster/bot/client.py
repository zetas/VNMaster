"""discord.py bot harness.

The on_raw_reaction_add handler routes reactions to vnmaster.bot.reactions.
Slash commands under /vnm wrap the pure handlers in vnmaster.bot.slash.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import Engine

from vnmaster.bot.apply_reaction import ReactionAction
from vnmaster.bot.reactions import build_interest_message, route_reaction
from vnmaster.bot.slash import (
    InvalidUrlError,
    NoSuchPairingError,
    cmd_pair,
    cmd_pairings_list,
    cmd_status,
    cmd_unpair,
)
from vnmaster.clock import Clock, SystemClock
from vnmaster.logging_setup import get_logger

log = get_logger(__name__)


class VNMasterBot(commands.Bot):
    def __init__(
        self,
        *,
        engine: Engine,
        channel_id: int,
        guild_id: int,
        config_path: Path,
        clock: Clock | None = None,
        skip_window_weeks: int = 4,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.dm_messages = True
        super().__init__(command_prefix="!vnm-unused-", intents=intents)
        self._engine = engine
        self._channel_id = channel_id
        self._guild_id = guild_id
        self._config_path = config_path
        self._clock = clock or SystemClock()
        self._skip_window_weeks = skip_window_weeks
        self._register_app_commands()

    def _register_app_commands(self) -> None:
        engine = self._engine
        clock = self._clock

        vnm = app_commands.Group(name="vnm", description="VNMaster commands")

        @vnm.command(name="status", description="Show last digest run and library stats")
        async def status_cmd(interaction: discord.Interaction) -> None:
            try:
                msg = cmd_status(engine=engine)
            except Exception as e:
                log.exception("status command failed")
                await interaction.response.send_message(
                    f"Status failed: {e}", ephemeral=True
                )
                return
            await interaction.response.send_message(msg, ephemeral=True)

        @vnm.command(
            name="pair",
            description="Manually pair a save dir / folder name to an F95 thread URL",
        )
        @app_commands.describe(
            name="The save dir name or folder name to pair",
            f95_url="The F95 thread URL (e.g. https://f95zone.to/threads/eternum.12345/)",
        )
        async def pair_cmd(
            interaction: discord.Interaction, name: str, f95_url: str
        ) -> None:
            try:
                msg = cmd_pair(
                    engine=engine, name=name, f95_url=f95_url,
                    now_epoch=clock.now_epoch(),
                )
            except InvalidUrlError as e:
                await interaction.response.send_message(
                    f"Bad URL: {e}", ephemeral=True
                )
                return
            except Exception as e:
                log.exception("pair command failed")
                await interaction.response.send_message(
                    f"Pair failed: {e}", ephemeral=True
                )
                return
            await interaction.response.send_message(msg, ephemeral=True)

        @vnm.command(name="pairings", description="List all save-folder → F95 thread pairings")
        async def pairings_cmd(interaction: discord.Interaction) -> None:
            try:
                msg = cmd_pairings_list(engine=engine)
            except Exception as e:
                log.exception("pairings command failed")
                await interaction.response.send_message(
                    f"Pairings failed: {e}", ephemeral=True
                )
                return
            await interaction.response.send_message(msg, ephemeral=True)

        @vnm.command(name="unpair", description="Remove a pairing by save dir, folder name, or thread id")
        @app_commands.describe(name="The save dir name, folder name, or numeric F95 thread id to remove")
        async def unpair_cmd(interaction: discord.Interaction, name: str) -> None:
            try:
                msg = cmd_unpair(engine=engine, name=name)
            except NoSuchPairingError:
                await interaction.response.send_message(
                    f"No pairing found for {name!r}", ephemeral=True
                )
                return
            except Exception as e:
                log.exception("unpair command failed")
                await interaction.response.send_message(
                    f"Unpair failed: {e}", ephemeral=True
                )
                return
            await interaction.response.send_message(msg, ephemeral=True)

        self.tree.add_command(vnm, guild=discord.Object(id=self._guild_id))

    async def setup_hook(self) -> None:
        # Sync to the configured guild — instant; global sync would take ~1 hour.
        guild = discord.Object(id=self._guild_id)
        synced = await self.tree.sync(guild=guild)
        log.info("synced %d slash commands to guild %d", len(synced), self._guild_id)

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.channel_id != self._channel_id:
            return
        if payload.user_id == (self.user.id if self.user else 0):
            return
        emoji = str(payload.emoji)
        result = route_reaction(
            engine=self._engine,
            message_id=str(payload.message_id),
            emoji=emoji,
            now_epoch=self._clock.now_epoch(),
            skip_window_weeks=self._skip_window_weeks,
        )
        if (
            result is not None
            and result.kind == "update"
            and result.action is ReactionAction.INTERESTED
        ):
            text = build_interest_message(
                engine=self._engine, thread_id=result.thread_id
            )
            if text is not None:
                try:
                    channel = self.get_channel(
                        self._channel_id
                    ) or await self.fetch_channel(self._channel_id)
                    message_channel = cast(
                        discord.TextChannel | discord.Thread,
                        channel,
                    )
                    await message_channel.send(
                        f"<@{payload.user_id}> {text}",
                        reference=discord.MessageReference(
                            message_id=payload.message_id,
                            channel_id=self._channel_id,
                            fail_if_not_exists=False,
                        ),
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except Exception:
                    log.warning(
                        "failed to post interest link for thread %s",
                        result.thread_id,
                    )


def run_bot(
    *,
    token: str,
    engine: Engine,
    channel_id: int,
    guild_id: int,
    config_path: Path,
) -> None:
    bot = VNMasterBot(
        engine=engine,
        channel_id=channel_id,
        guild_id=guild_id,
        config_path=config_path,
    )
    asyncio.run(bot.start(token))
