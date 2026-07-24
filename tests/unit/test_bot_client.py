"""Smoke tests for VNMasterBot construction and slash command registration.

We don't actually start the bot (no gateway connection in tests). We just
verify the command tree has the expected entries — so we catch typos or
registration bugs before they hit Discord.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine

from vnmaster.bot.client import VNMasterBot
from vnmaster.db.models import Base


def _bot_for_tests(tmp_path: Path) -> VNMasterBot:
    engine = create_engine(f"sqlite:///{tmp_path / 'v.db'}", future=True)
    Base.metadata.create_all(engine)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    return VNMasterBot(
        engine=engine,
        channel_id=1,
        guild_id=1234567890,
        config_path=cfg_path,
    )


def _collect_command_names(bot: VNMasterBot) -> set[str]:
    """Walk the bot's command tree and collect fully-qualified command names."""
    import discord
    guild = discord.Object(id=bot._guild_id)
    names: set[str] = set()

    def walk(node, prefix: str = "") -> None:
        try:
            children = node.commands
        except AttributeError:
            children = []
        for child in children:
            full = f"{prefix}{child.name}"
            names.add(full)
            walk(child, prefix=f"{full} ")

    for cmd in bot.tree.get_commands(guild=guild):
        names.add(cmd.name)
        walk(cmd, prefix=f"{cmd.name} ")
    return names


def test_bot_registers_vnm_group(tmp_path: Path) -> None:
    bot = _bot_for_tests(tmp_path)
    names = _collect_command_names(bot)
    assert "vnm" in names


def test_bot_registers_status_and_pair(tmp_path: Path) -> None:
    bot = _bot_for_tests(tmp_path)
    names = _collect_command_names(bot)
    assert "vnm status" in names
    assert "vnm pair" in names


def test_bot_does_not_register_tags_subgroup(tmp_path: Path) -> None:
    bot = _bot_for_tests(tmp_path)
    names = _collect_command_names(bot)
    assert "vnm tags" not in names
    assert "vnm tags add" not in names
    assert "vnm tags exclude" not in names
    assert "vnm tags remove" not in names
    assert "vnm tags list" not in names


def test_bot_registers_pairings_and_unpair(tmp_path: Path) -> None:
    bot = _bot_for_tests(tmp_path)
    names = _collect_command_names(bot)
    assert "vnm pairings" in names
    assert "vnm unpair" in names
