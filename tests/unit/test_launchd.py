from pathlib import Path

import pytest

from vnmaster.launchd import (
    InvalidCronError, parse_simple_cron, render_weekly_plist, render_bot_plist,
)


def test_parse_cron_extracts_minute_hour_weekday() -> None:
    parsed = parse_simple_cron("0 9 * * SAT")
    assert parsed.minute == 0
    assert parsed.hour == 9
    assert parsed.weekday == 6  # launchd convention: SAT=6


def test_parse_cron_rejects_complex_expressions() -> None:
    with pytest.raises(InvalidCronError):
        parse_simple_cron("*/5 * * * *")


def test_render_weekly_plist_substitutes_placeholders(tmp_path: Path) -> None:
    out = render_weekly_plist(
        bin_path=Path("/opt/bin/vnmaster"),
        log_dir=Path("/Users/x/Logs"),
        cron="0 9 * * SAT",
    )
    assert "/opt/bin/vnmaster" in out
    assert "/Users/x/Logs" in out
    assert "<integer>9</integer>" in out
    assert "<integer>0</integer>" in out


def test_render_bot_plist(tmp_path: Path) -> None:
    out = render_bot_plist(
        bin_path=Path("/opt/bin/vnmaster"),
        log_dir=Path("/Users/x/Logs"),
    )
    assert "/opt/bin/vnmaster" in out
    assert "<true/>" in out  # KeepAlive


def test_parse_cron_accepts_wildcard_weekday() -> None:
    from vnmaster.launchd import parse_simple_cron
    parsed = parse_simple_cron("0 1 * * *")
    assert parsed.minute == 0
    assert parsed.hour == 1
    assert parsed.weekday is None


def test_render_daily_plist_has_no_weekday_and_uses_daily_flag(tmp_path) -> None:
    from vnmaster.launchd import render_daily_plist
    out = render_daily_plist(
        bin_path=Path("/opt/bin/vnmaster"),
        log_dir=Path("/Users/x/Logs"),
        cron="0 1 * * *",
    )
    assert "dev.vnmaster.daily" in out
    assert "<string>--daily</string>" in out
    assert "<key>Weekday</key>" not in out
    assert "<integer>1</integer>" in out  # hour


def test_install_daily_plist_writes_file(tmp_path) -> None:
    from vnmaster.launchd import install_daily_plist
    p = install_daily_plist(daily_text="<plist/>", launchagents_dir=tmp_path)
    assert p.name == "dev.vnmaster.daily.plist"
    assert p.read_text() == "<plist/>"
