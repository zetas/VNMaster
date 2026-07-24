from pathlib import Path

import pytest

import tomllib

from vnmaster.config import Config, ConfigError, Secrets, load_runtime_settings


FIXTURES = Path(__file__).parent.parent / "fixtures" / "configs"


def test_config_loads_valid_toml(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config.load(FIXTURES / "valid.toml")
    assert cfg.paths.games_root == tmp_path / "Games"
    assert cfg.discord.guild_id == "123456789"
    assert cfg.discord.webhook_url is None
    assert cfg.anthropic.model == "claude-haiku-4-5"
    assert cfg.matching.fuzzy_threshold == 90
    assert cfg.downloads.destination == tmp_path / "Games"
    assert cfg.downloads.platform_priority == ["mac", "windows", "linux"]
    assert cfg.magnitude_score.renders == 1.0


def test_config_missing_required_field_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Config.load(FIXTURES / "missing_required.toml")


def test_secrets_chmod_warning(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        'discord_bot_token = "tok"\nanthropic_api_key = "key"\n'
    )
    secrets_file.chmod(0o644)
    with pytest.warns(UserWarning, match="permissions are too open"):
        Secrets.load(secrets_file)


def test_secrets_with_correct_permissions(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        'discord_bot_token = "tok"\nanthropic_api_key = "key"\n'
    )
    secrets_file.chmod(0o600)
    s = Secrets.load(secrets_file)
    assert s.discord_bot_token == "tok"
    assert s.anthropic_api_key == "key"
    assert s.discord_webhook_url is None
    assert s.f95zone_cookies is None


def test_secrets_includes_optional_f95zone_cookies(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        'discord_bot_token = "tok"\n'
        'anthropic_api_key = "key"\n'
        'f95zone_cookies = "xf_user=abc; xf_session=def"\n'
    )
    secrets_file.chmod(0o600)
    s = Secrets.load(secrets_file)
    assert s.f95zone_cookies == "xf_user=abc; xf_session=def"


def test_schedule_config_daily_cron_default() -> None:
    from vnmaster.config import ScheduleConfig
    assert ScheduleConfig().daily_cron == "0 1 * * *"


def test_runtime_settings_migrate_legacy_webhook_to_secrets(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[paths]
games_root = "~/Games"
renpy_saves_root = "~/Library/RenPy"
f95checker_db = "~/f95.db"
vnmaster_db = "~/vnmaster.db"

[discord]
guild_id = "123"
channel_id = "456"
webhook_url = "https://example.invalid/legacy-webhook"

[anthropic]
model = "claude-haiku-4-5"
monthly_budget_usd = 5.0
""".strip()
    )
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        'discord_bot_token = "tok"\nanthropic_api_key = "key"\n'
    )
    secrets_file.chmod(0o600)

    cfg, secrets = load_runtime_settings(config_file, secrets_file)

    assert cfg.discord.webhook_url is None
    assert secrets.discord_webhook_url == "https://example.invalid/legacy-webhook"
    assert "webhook_url" not in tomllib.loads(config_file.read_text())["discord"]
    assert config_file.stat().st_mode & 0o777 == 0o600
    assert secrets_file.stat().st_mode & 0o777 == 0o600


def test_runtime_settings_require_secret_webhook(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text((FIXTURES / "valid.toml").read_text())
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        'discord_bot_token = "tok"\nanthropic_api_key = "key"\n'
    )
    secrets_file.chmod(0o600)

    with pytest.raises(ConfigError, match="webhook URL is missing"):
        load_runtime_settings(config_file, secrets_file)
