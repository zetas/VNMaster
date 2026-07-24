"""Render and install launchd plists for the weekly digest + bot daemon.

Templates are inlined as module constants rather than read from
scripts/plists/ on disk — that directory isn't packaged into the wheel,
so a file-path lookup breaks once vnmaster is installed via uv tool.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# launchd Weekday convention: 0 or 7 = Sunday; 1 = Monday; ...; 6 = Saturday.
_WEEKDAY_MAP = {
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6,
}

_WEEKLY_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.vnmaster.weekly</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{VNMASTER_BIN}}</string>
    <string>digest</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>{{WEEKDAY}}</integer>
    <key>Hour</key>
    <integer>{{HOUR}}</integer>
    <key>Minute</key>
    <integer>{{MINUTE}}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{{LOG_DIR}}/weekly.out.log</string>
  <key>StandardErrorPath</key>
  <string>{{LOG_DIR}}/weekly.err.log</string>
</dict>
</plist>
"""

_BOT_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.vnmaster.bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{VNMASTER_BIN}}</string>
    <string>bot</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{{LOG_DIR}}/bot.out.log</string>
  <key>StandardErrorPath</key>
  <string>{{LOG_DIR}}/bot.err.log</string>
</dict>
</plist>
"""

_DAILY_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.vnmaster.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{VNMASTER_BIN}}</string>
    <string>digest</string>
    <string>--daily</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{{HOUR}}</integer>
    <key>Minute</key>
    <integer>{{MINUTE}}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{{LOG_DIR}}/daily.out.log</string>
  <key>StandardErrorPath</key>
  <string>{{LOG_DIR}}/daily.err.log</string>
</dict>
</plist>
"""


class InvalidCronError(ValueError):
    pass


@dataclass(frozen=True)
class SimpleCron:
    minute: int
    hour: int
    weekday: int | None  # None = every day (weekday field was '*')


_CRON_RE = re.compile(
    r"^(?P<minute>\d+)\s+(?P<hour>\d+)\s+\*\s+\*\s+(?P<weekday>[A-Z]{3}|\*)$"
)


def parse_simple_cron(expr: str) -> SimpleCron:
    m = _CRON_RE.match(expr.strip())
    if not m:
        raise InvalidCronError(
            "vnmaster supports only `M H * * DAY` cron expressions where DAY is "
            "SUN-SAT or * (every day)"
        )
    wd_token = m.group("weekday")
    # "*" is the cron wildcard, not a credential.
    if wd_token == "*":  # nosec B105
        weekday: int | None = None
    else:
        weekday = _WEEKDAY_MAP.get(wd_token)
        if weekday is None:
            raise InvalidCronError(f"unknown weekday: {wd_token!r}")
    return SimpleCron(
        minute=int(m.group("minute")),
        hour=int(m.group("hour")),
        weekday=weekday,
    )


def render_weekly_plist(*, bin_path: Path, log_dir: Path, cron: str) -> str:
    parsed = parse_simple_cron(cron)
    return (
        _WEEKLY_PLIST_TEMPLATE
        .replace("{{VNMASTER_BIN}}", str(bin_path))
        .replace("{{LOG_DIR}}", str(log_dir))
        .replace("{{WEEKDAY}}", str(parsed.weekday))
        .replace("{{HOUR}}", str(parsed.hour))
        .replace("{{MINUTE}}", str(parsed.minute))
    )


def render_bot_plist(*, bin_path: Path, log_dir: Path) -> str:
    return (
        _BOT_PLIST_TEMPLATE
        .replace("{{VNMASTER_BIN}}", str(bin_path))
        .replace("{{LOG_DIR}}", str(log_dir))
    )


def render_daily_plist(*, bin_path: Path, log_dir: Path, cron: str) -> str:
    parsed = parse_simple_cron(cron)
    return (
        _DAILY_PLIST_TEMPLATE
        .replace("{{VNMASTER_BIN}}", str(bin_path))
        .replace("{{LOG_DIR}}", str(log_dir))
        .replace("{{HOUR}}", str(parsed.hour))
        .replace("{{MINUTE}}", str(parsed.minute))
    )


def install_plists(
    *, weekly_text: str, bot_text: str, launchagents_dir: Path
) -> tuple[Path, Path]:
    launchagents_dir.mkdir(parents=True, exist_ok=True)
    weekly = launchagents_dir / "dev.vnmaster.weekly.plist"
    bot = launchagents_dir / "dev.vnmaster.bot.plist"
    weekly.write_text(weekly_text)
    bot.write_text(bot_text)
    return weekly, bot


def install_daily_plist(*, daily_text: str, launchagents_dir: Path) -> Path:
    launchagents_dir.mkdir(parents=True, exist_ok=True)
    daily = launchagents_dir / "dev.vnmaster.daily.plist"
    daily.write_text(daily_text)
    return daily
