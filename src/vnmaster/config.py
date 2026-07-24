"""Strongly-typed configuration loaded from TOML files.

Two files:
  ~/.config/vnmaster/config.toml   — non-secret settings
  ~/.config/vnmaster/secrets.toml  — API keys/tokens; must be chmod 600
"""
from __future__ import annotations

import os
import stat
import tempfile
import tomllib
import warnings
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field, ValidationError, field_validator

from vnmaster.paths import VNMasterPaths


class ConfigError(Exception):
    """Raised when the config file cannot be parsed or is missing fields."""


class PathsConfig(BaseModel):
    games_root: Path
    renpy_saves_root: Path
    f95checker_db: Path
    vnmaster_db: Path

    @field_validator("*", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return VNMasterPaths.expand_user(Path(v))
        return v


class DiscordConfig(BaseModel):
    guild_id: str
    channel_id: str
    # Read-only compatibility for configurations created before webhook URLs
    # moved into secrets.toml. Runtime loading migrates and removes this field.
    webhook_url: str | None = None


class AnthropicConfig(BaseModel):
    model: str = "claude-haiku-4-5"
    monthly_budget_usd: float = Field(gt=0.0)


class ScheduleConfig(BaseModel):
    cron: str = "0 9 * * SAT"
    daily_cron: str = "0 1 * * *"


class MatchingConfig(BaseModel):
    fuzzy_threshold: int = Field(default=85, ge=0, le=100)


class DownloadsConfig(BaseModel):
    """Local download preferences for ``vnmaster fetch``."""

    destination: Path = Field(default_factory=lambda: Path(os.environ["HOME"]) / "Games")
    platform_priority: list[str] = Field(
        default_factory=lambda: ["mac", "windows", "linux"]
    )
    preferred_hosts: list[str] = Field(default_factory=lambda: ["mega"])

    @field_validator("destination", mode="before")
    @classmethod
    def _expand_destination(cls, v: object) -> object:
        if isinstance(v, str):
            return VNMasterPaths.expand_user(Path(v))
        return v


class MagnitudeScoreConfig(BaseModel):
    """Weights that convert content metrics into an *estimated added playtime*
    (in hours). The star rating roughly equals this hour estimate.

    Only length-bearing metrics count: renders, words, and animations. Scenes,
    locations, and characters say nothing about runtime, so they default to 0.
    Anchor: ~1000 renders ≈ 1 hour, ~10k words ≈ 1 hour, ~bugfix-only ≈ 0 hours.
    """

    renders: float = 0.001       # 1000 renders ≈ 1 hour
    animations: float = 0.005    # ≈18s each — 5× a still render
    words_per_1k: float = 0.1    # 1000 words ≈ 0.1h → ~10k words/hour
    scenes: float = 0.0          # ignored: no bearing on runtime
    new_locations: float = 0.0   # ignored
    new_characters: float = 0.0  # ignored
    bugfix_only_penalty: float = 0.0  # a bugfix-only version adds no playtime


class Config(BaseModel):
    paths: PathsConfig
    discord: DiscordConfig
    anthropic: AnthropicConfig
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    downloads: DownloadsConfig = Field(default_factory=DownloadsConfig)
    magnitude_score: MagnitudeScoreConfig = Field(default_factory=MagnitudeScoreConfig)

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = tomllib.loads(path.read_text())
            return cls.model_validate(raw)
        except (tomllib.TOMLDecodeError, ValidationError) as e:
            raise ConfigError(f"failed to load {path}: {e}") from e


class Secrets(BaseModel):
    discord_bot_token: str
    discord_webhook_url: str | None = None
    anthropic_api_key: str
    # Raw `Cookie:` header value from a logged-in F95Zone browser session.
    # Required for wizard-time F95Zone search; unused at runtime.
    f95zone_cookies: str | None = None

    @property
    def required_discord_webhook_url(self) -> str:
        if not self.discord_webhook_url:
            raise ConfigError(
                "Discord webhook URL is missing from secrets.toml; "
                "run `vnmaster init` to configure it"
            )
        return self.discord_webhook_url

    @classmethod
    def load(cls, path: Path) -> "Secrets":
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
            warnings.warn(
                f"{path} permissions are too open — chmod 600 recommended",
                UserWarning,
                stacklevel=2,
            )
        try:
            raw = tomllib.loads(path.read_text())
            return cls.model_validate(raw)
        except (tomllib.TOMLDecodeError, ValidationError) as e:
            raise ConfigError(f"failed to load secrets {path}: {e}") from e


def load_runtime_settings(
    config_path: Path,
    secrets_path: Path,
) -> tuple[Config, Secrets]:
    """Load settings and securely migrate a legacy Discord webhook.

    Older VNMaster releases placed the bearer webhook URL in config.toml.
    The one-time migration moves it to secrets.toml, removes the legacy copy,
    and atomically writes both files with mode 0600.
    """
    cfg = Config.load(config_path)
    secrets = Secrets.load(secrets_path)
    legacy_webhook = cfg.discord.webhook_url
    if legacy_webhook is not None:
        _migrate_discord_webhook(
            config_path=config_path,
            secrets_path=secrets_path,
            legacy_webhook=legacy_webhook,
        )
        cfg = Config.load(config_path)
        secrets = Secrets.load(secrets_path)

    secrets.required_discord_webhook_url
    return cfg, secrets


def _migrate_discord_webhook(
    *,
    config_path: Path,
    secrets_path: Path,
    legacy_webhook: str,
) -> None:
    config_data = tomllib.loads(config_path.read_text())
    secrets_data = tomllib.loads(secrets_path.read_text())

    discord_section = config_data.get("discord")
    if not isinstance(discord_section, dict):
        raise ConfigError("config.toml is missing its [discord] section")
    discord_section.pop("webhook_url", None)
    secrets_data.setdefault("discord_webhook_url", legacy_webhook)

    _write_private_toml(config_path, config_data)
    _write_private_toml(secrets_path, secrets_data)


def write_private_toml(path: Path, data: dict[str, Any]) -> None:
    """Atomically write TOML that may contain credentials with mode 0600."""
    _write_private_toml(path, data)


def _write_private_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(tomli_w.dumps(data))
            destination.flush()
            os.fsync(destination.fileno())
        temporary_path.replace(path)
        path.chmod(0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
