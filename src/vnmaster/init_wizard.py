"""First-run interactive setup.

Wizard steps are described in spec §6. Most steps are I/O (click prompts, HTTP);
the testable bits are `generate_import_candidates`, `_save_dir_to_search_term`,
and `_build_candidate_rows` itself (with a synthetic F95 list).

The wizard is re-runnable. If config.toml and secrets.toml already exist, the
prompts pre-fill with their values so you can press Enter to keep each one
and only retype what changed.
"""
from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote_plus

import click
import httpx

from vnmaster.db.ro_f95checker import F95CheckerDB
from vnmaster.db.ro_f95checker import F95CheckerGame
from vnmaster.f95_search import (
    F95SearchHit,
    build_search_client,
    clean_thread_title,
    search_with_backoff,
)
from vnmaster.logging_setup import get_logger
from vnmaster.paths import VNMasterPaths
from vnmaster.scanners.disk import scan_disk
from vnmaster.scanners.play_history import scan_play_history
from vnmaster.scanners.types import InstalledGame, PlayHistoryEntry

log = get_logger(__name__)


# Matching thresholds for the candidate file.
HIGH_THRESHOLD = 85
MEDIUM_THRESHOLD = 70
# Below this we don't trust the match at all — emit a F95 search URL instead.
BAD_BELOW = 55

_TRAILING_TIMESTAMP_RE = re.compile(r"[-_]\d{8,}$")
_VERSION_SUFFIX_RE = re.compile(r"[-_](\d+\.\d+(\.\d+)?[a-z]?|pc|mac|linux|release)$", re.IGNORECASE)
# Two camelCase patterns combined:
#   "DayAt"   → "Day|At"   (lowercase followed by uppercase)
#   "ATime"   → "A|Time"   (uppercase followed by uppercase-then-lowercase)
# Together they turn "OneDayAtATime" into "One Day At A Time".
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# F95Zone's latest_alpha search drops these stop words during indexing but
# treats them as required terms when querying. Result: searching "Light of
# My Life" returns 0 hits, but "Light My Life" finds the right thread.
# Strip them from queries before sending.
_STOP_WORDS = {
    "a", "an", "the",
    "of", "in", "on", "at", "by", "for", "with", "to", "from",
    "and", "or", "but",
    "is", "are", "be", "as",
}

# Tokens devs commonly append to save_directory names that aren't part of
# the game title. Removing them often un-blocks the search.
_DEV_NOISE_TOKENS = {
    "save", "saves", "savedata", "savegame", "savegames",
    "full", "demo", "beta", "alpha", "preview", "test", "tests",
    "ea", "early", "access", "public", "release",
    "rewrite", "remake", "reboot", "redux", "reimagined", "remastered",
    "complete", "edition", "definitive", "extended",
    "ch", "chp", "chapter", "ep", "episode", "pt", "part", "season",
    "v", "vol", "volume", "ver", "version",
}
# Match: trailing CHP1, Part2, S2, PT01, EA, Full, etc.
_DEV_SUFFIX_RE = re.compile(
    r"(?i)(?:[-_]|(?<=[a-z]))("
    r"saves?|full|demo|beta|public|rewrite|remake|reboot|complete|extended|"
    r"(?:ch|chp|chapter|pt|part|ep|episode|s|season)\d+|"
    r"v\d+(?:\.\d+)*|ea\d*"
    r")(?=[-_ ]|$)"
)

# Ren'Py engine writes per-user persistent + tutorial + launcher directories
# under ~/Library/RenPy/ that aren't actual games. Skip them so the wizard
# doesn't waste search queries trying to find them.
_SKIP_SAVE_DIRS = {
    "persistent", "tokens", "launcher", "save_token",
}
_SKIP_SAVE_DIR_PREFIXES = ("tutorial-", "launcher-")

# Tokens this short are usually noise — version letters, suffixes,
# Ren'Py-internal markers. Drop from the search query.
_MIN_TOKEN_LEN = 2

_wordsegment_loaded = False


def _word_segment(token: str) -> list[str]:
    """Split a concatenated-lowercase token into known English words.

    Note: wordsegment is statistical; its unigram corpus contains
    enough common 2-letter "words" ("hr", "al", "de") that a frequency
    or dictionary check doesn't reliably reject fake-name splits like
    "Talothral" → "lot hr al". We rely on the caller to gate this
    function with a length threshold that excludes most real single
    English words (which rarely exceed 11 characters).

    Wordsegment loads a multi-megabyte unigram corpus on first use;
    lazy-loaded once per process.
    """
    global _wordsegment_loaded
    if not _wordsegment_loaded:
        from wordsegment import load as _ws_load  # type: ignore[import-untyped]
        _ws_load()
        _wordsegment_loaded = True
    from wordsegment import segment
    return cast(list[str], segment(token)) or [token]


def _should_skip_save_dir(name: str) -> bool:
    """True if this save dir is a Ren'Py engine internal, not a game."""
    if name in _SKIP_SAVE_DIRS:
        return True
    for prefix in _SKIP_SAVE_DIR_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def generate_import_candidates(
    rows: Iterable[tuple[str, str, str, str | None]],
) -> str:
    """Render an import-candidates.txt file body.

    Each row: (confidence_label, game_title, hint, f95_url_or_search_url_or_None)
    Confidence order: high → medium → low → none (unmatched).
    """
    order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    sorted_rows = sorted(rows, key=lambda r: order.get(r[0].lower(), 4))
    if not sorted_rows:
        return "# No candidates found.\n"
    lines = [
        "# F95Checker import candidates",
        "# Review each row. HIGH confidence rows can usually be pasted into",
        "# F95Checker's URL-import dialog as-is. LOW and NONE rows include a",
        "# F95Zone search URL — open it, find the right thread, copy that URL.",
        "",
    ]
    for confidence, title, hint, url in sorted_rows:
        prefix = f"# {confidence.upper():<7s} | {title} | {hint}"
        lines.append(prefix)
        lines.append(url or "# (no URL — search F95Zone manually)")
        lines.append("")
    return "\n".join(lines)


def _save_dir_to_search_term(save_dir_name: str) -> str:
    """Convert a Ren'Py save_directory name to a plausible F95Zone search query.

    Pipeline:
      1. Strip trailing timestamp and version suffix.
      2. CamelCase-split: "OneDayAtATime" → "One Day At A Time".
      3. Replace dashes/underscores with spaces.
      4. For each remaining all-lowercase token longer than 8 chars
         (i.e., looks like a concatenated phrase: "mybrotherswife"),
         run wordsegment to split it into English words.
      5. Drop English stop words ("of", "a", "the", …) — F95Zone's
         latest_alpha API rejects queries that contain them.
      6. Drop short noise tokens (single-character version letters etc.).
    """
    base = _TRAILING_TIMESTAMP_RE.sub("", save_dir_name)
    base = _VERSION_SUFFIX_RE.sub("", base)
    # Strip dev-added suffix tokens (Part2, CHP1, Save, Full, Rewrite, …)
    # before camelCase splitting so we don't end up with stale token noise.
    while True:
        stripped = _DEV_SUFFIX_RE.sub(" ", base).strip()
        if stripped == base.strip():
            break
        base = stripped
    base = _CAMEL_SPLIT_RE.sub(" ", base)
    base = base.replace("-", " ").replace("_", " ").strip()
    if not base:
        return save_dir_name

    # Word-segment tokens that look like concatenated English phrases.
    # The 12-char threshold is chosen to preserve real single English
    # words (which rarely exceed 11 chars — "Despondence" is 11,
    # "Talothral" is 9) while catching concatenated phrases like
    # "Mybrotherswife" (14) and "sixteenyearslater" (17).
    expanded: list[str] = []
    for token in base.split():
        if len(token) >= 12 and not any(c.isdigit() for c in token):
            try:
                segments = _word_segment(token.lower())
            except Exception:
                segments = [token]
            if len(segments) >= 2:
                expanded.extend(segments)
            else:
                expanded.append(token)
        else:
            expanded.append(token)

    # Split tokens that end with embedded "of": "Thiefof" → ["Thief", "of"].
    # Restricted to "of" only because broader patterns (in/to/for/...)
    # mangle real English words like "Again" → ["Aga", "in"].
    split_again: list[str] = []
    for t in expanded:
        lower = t.lower()
        if lower.endswith("of") and len(lower) >= 6:
            split_again.append(t[:-2])
            split_again.append("of")
        else:
            split_again.append(t)

    tokens = [
        t for t in split_again
        if t.lower() not in _STOP_WORDS
        and t.lower() not in _DEV_NOISE_TOKENS
        and len(t) >= _MIN_TOKEN_LEN
        and not t.isdigit()
    ]
    if not tokens:
        return base
    return " ".join(tokens)


def _f95_search_url(query: str) -> str:
    return f"https://f95zone.to/search/search/?q={quote_plus(query)}&t=post&c[child_nodes]=1&c[nodes][0]=2&o=relevance"


def run_wizard(paths: VNMasterPaths) -> None:
    """Interactive wizard. Each step shells out to already-tested helpers."""
    from anthropic import Anthropic

    click.echo("VNMaster setup wizard")
    click.echo("=" * 40)

    existing_config = _load_existing_toml(paths.config_dir / "config.toml")
    existing_secrets = _load_existing_toml(paths.config_dir / "secrets.toml")

    if existing_config or existing_secrets:
        click.echo(
            "Found existing config — values shown in [brackets] will be kept if you press Enter."
        )

    cfg_paths = existing_config.get("paths", {})
    cfg_discord = existing_config.get("discord", {})

    # 1. Confirm paths
    games_root = click.prompt(
        "Games folder",
        default=cfg_paths.get("games_root") or str(paths.games_root),
    )
    saves_root = click.prompt(
        "Ren'Py saves folder",
        default=cfg_paths.get("renpy_saves_root") or str(paths.renpy_saves_root),
    )
    f95_db_path = click.prompt(
        "F95Checker DB path",
        default=cfg_paths.get("f95checker_db") or str(paths.f95checker_db),
    )

    # 2. Verify F95Checker
    if not Path(f95_db_path).exists():
        click.echo(
            f"F95Checker DB not found at {f95_db_path}. Install F95Checker "
            "from https://github.com/WillyJL/F95Checker, add at least one "
            "game, then re-run `vnmaster init`."
        )
        raise SystemExit(1)

    # 3. Anthropic key
    anthropic_key = _prompt_secret_with_existing(
        label="Anthropic API key",
        existing=existing_secrets.get("anthropic_api_key"),
    )
    click.echo("Testing Anthropic key...", nl=False)
    test_client = Anthropic(api_key=anthropic_key)
    test_client.messages.create(
        model="claude-haiku-4-5", max_tokens=1,
        messages=[{"role": "user", "content": "hi"}],
    )
    click.echo(" ok.")

    # 4. Discord
    discord_token = _prompt_secret_with_existing(
        label="Discord bot token",
        existing=existing_secrets.get("discord_bot_token"),
    )
    guild_id = click.prompt(
        "Discord guild (server) ID", default=cfg_discord.get("guild_id"),
    )
    channel_id = click.prompt(
        "Discord channel ID", default=cfg_discord.get("channel_id"),
    )
    webhook_url = click.prompt(
        "Discord webhook URL",
        default=(
            existing_secrets.get("discord_webhook_url")
            or cfg_discord.get("webhook_url")
        ),
    )

    # 4b. F95Zone cookies (for authenticated search)
    f95zone_cookies = _prompt_f95zone_cookies(
        existing=existing_secrets.get("f95zone_cookies"),
    )

    # 4c. PERSIST CREDENTIALS NOW.
    # Everything below this point can take minutes (scanner + 67 search calls)
    # or fail (F95Zone HTML changes, network blip). If we wait until the end
    # to write secrets.toml, an abort mid-way means the user re-enters all
    # credentials next run. Save now so re-runs short-circuit the prompts.
    _save_credentials_early(
        paths=paths,
        anthropic_key=anthropic_key,
        discord_token=discord_token,
        discord_webhook_url=webhook_url,
        f95zone_cookies=f95zone_cookies,
        cfg_paths_section={
            "games_root": games_root,
            "renpy_saves_root": saves_root,
            "f95checker_db": f95_db_path,
            "vnmaster_db": cfg_paths.get("vnmaster_db") or str(paths.vnmaster_db),
        },
        cfg_discord_section={
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
        },
        existing_config=existing_config,
    )
    click.echo(
        f"Credentials saved to {paths.config_dir / 'secrets.toml'}. "
        "If the next step crashes, your inputs are persisted."
    )

    # 5. Run scanners
    play = scan_play_history(Path(saves_root))
    installed = scan_disk(Path(games_root))
    click.echo(f"Found {len(play)} played games, {len(installed)} installed.")

    # 6. Generate candidates
    f95 = F95CheckerDB.open(Path(f95_db_path))
    f95.check_schema()
    f95_rows = list(f95.iter_all_games())
    click.echo(f"F95Checker DB has {len(f95_rows)} games tracked.")
    if f95zone_cookies:
        click.echo(
            "Searching F95Zone for each save dir not already tracked. "
            "This takes ~0.6s per query."
        )
    else:
        click.echo(
            "Skipping F95Zone search (no cookies provided). Every save dir "
            "without a local match will emit a manual-search URL."
        )

    def _progress(idx: int, total: int, name: str) -> None:
        click.echo(f"  [{idx}/{total}] {name}")

    rows = _build_candidate_rows(
        play, installed, f95_rows,
        progress_cb=_progress,
        cookie_header=f95zone_cookies,
    )
    candidates_path = paths.config_dir / "import-candidates.txt"
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    candidates_path.write_text(generate_import_candidates(rows))

    high = sum(1 for r in rows if r[0] == "high")
    medium = sum(1 for r in rows if r[0] == "medium")
    low = sum(1 for r in rows if r[0] == "low")
    none_count = sum(1 for r in rows if r[0] == "none")
    click.echo(
        f"Import candidates written to {candidates_path}\n"
        f"  HIGH: {high}  MEDIUM: {medium}  LOW: {low}  NONE: {none_count}\n"
        f"HIGH rows are safe to paste into F95Checker as-is. NONE rows need\n"
        f"manual F95Zone search (URL in the file)."
    )
    click.confirm(
        "Paste the URLs you trust into F95Checker, then press y to continue",
        abort=True,
    )

    # 7. Config + secrets were already written in step 4c. No-op here.

    # 8. Install launchd jobs
    from vnmaster.launchd import (
        install_daily_plist, install_plists, render_bot_plist,
        render_daily_plist, render_weekly_plist,
    )
    import shutil
    bin_path = Path(shutil.which("vnmaster") or "vnmaster")
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    weekly = render_weekly_plist(
        bin_path=bin_path, log_dir=paths.log_dir, cron="0 9 * * SAT"
    )
    bot = render_bot_plist(bin_path=bin_path, log_dir=paths.log_dir)
    launchagents = Path.home() / "Library" / "LaunchAgents"
    weekly_path, bot_path = install_plists(
        weekly_text=weekly, bot_text=bot, launchagents_dir=launchagents
    )
    daily = render_daily_plist(
        bin_path=bin_path, log_dir=paths.log_dir, cron="0 1 * * *"
    )
    daily_path = install_daily_plist(daily_text=daily, launchagents_dir=launchagents)
    click.echo(f"Installed launchd plists: {weekly_path}, {daily_path}, {bot_path}\n")
    click.echo(
        "Load them with the modern launchctl syntax (NOT `launchctl load`,\n"
        "which is deprecated and gives cryptic I/O errors):\n\n"
        f"  launchctl bootstrap gui/$(id -u) {bot_path}\n"
        f"  launchctl bootstrap gui/$(id -u) {weekly_path}\n"
        f"  launchctl bootstrap gui/$(id -u) {daily_path}\n\n"
        "To stop/remove later:\n"
        f"  launchctl bootout gui/$(id -u) {bot_path}\n"
        f"  launchctl bootout gui/$(id -u) {weekly_path}\n"
        f"  launchctl bootout gui/$(id -u) {daily_path}"
    )

    click.echo(
        "\nSetup complete. Run `vnmaster digest` to send your first digest."
    )


def _load_existing_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _prompt_secret_with_existing(*, label: str, existing: str | None) -> str:
    """Prompt for a secret, or reuse the existing value with one-tap confirm."""
    if existing:
        masked = existing[:4] + "…" + existing[-4:] if len(existing) > 10 else "(hidden)"
        if click.confirm(
            f"Use existing {label} ({masked})?", default=True
        ):
            return existing
    return str(click.prompt(label, hide_input=True))


def _prompt_f95zone_cookies(*, existing: str | None) -> str | None:
    """Prompt for the user's logged-in F95Zone Cookie header. Optional."""
    if existing:
        masked = f"len={len(existing)}"
        if click.confirm(
            f"Use existing F95Zone cookies ({masked})?", default=True
        ):
            return existing
    click.echo(
        "F95Zone search requires authentication. To populate your library\n"
        "automatically:\n"
        "  1. Log into https://f95zone.to in your browser.\n"
        "  2. Open DevTools (Cmd-Option-I) → Network tab → reload the page.\n"
        "  3. Click any request to f95zone.to → Headers → find 'Cookie:'.\n"
        "  4. Copy the entire cookie header value and paste below.\n"
        "Press Enter to skip (every save dir will fall back to a manual\n"
        "search URL instead).\n"
    )
    value = click.prompt(
        "F95Zone Cookie header (or Enter to skip)",
        default="", show_default=False, hide_input=True,
    ).strip()
    return value or None


def _build_candidate_rows(
    play_history: Iterable[PlayHistoryEntry],
    installed: Iterable[InstalledGame],
    f95_rows: Iterable[F95CheckerGame],
    *,
    search_fn: Callable[[str], list[F95SearchHit]] | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
    cookie_header: str | None = None,
) -> list[tuple[str, str, str, str | None]]:
    """Cross-reference scanner outputs against F95Checker locally, then F95Zone.

    For each save dir / installed folder:
      1. If F95Checker already has a HIGH-confidence local match, use it
         (no network call).
      2. Otherwise, search F95Zone for the title. If the top hit scores
         HIGH against the cleaned save-dir name, emit it as HIGH; if
         MEDIUM, emit MEDIUM; if LOW or no result, emit NONE with a
         manual-search URL.

    `search_fn` is injected for testability (defaults to live F95Zone
    search using build_search_client). `progress_cb(idx, total, name)`
    is called once per name before the search runs.
    """
    from rapidfuzz import fuzz

    f95_local = {f.name: f.id for f in f95_rows}
    rows: list[tuple[str, str, str, str | None]] = []
    seen: set[str] = set()

    # Deduplicate names up front so progress reporting is accurate.
    # Skip Ren'Py engine-internal directories that aren't games.
    entries: list[tuple[str, str]] = []
    for p in play_history:
        if p.save_dir_name not in seen and not _should_skip_save_dir(p.save_dir_name):
            seen.add(p.save_dir_name)
            entries.append((p.save_dir_name, "from ~/Library/RenPy/"))
    for i in installed:
        if i.folder_name not in seen and not _should_skip_save_dir(i.folder_name):
            seen.add(i.folder_name)
            entries.append((i.folder_name, "from ~/Games/"))

    # Lazy-init the http client only if we'll actually need to search.
    client: httpx.Client | None = None
    # If no cookies, refuse to search live — every entry gets a manual URL.
    search_enabled = search_fn is not None or bool(cookie_header)
    # Circuit breaker: after 3 consecutive HTTP failures we assume the
    # remote is rate-limiting/blocking us and stop attempting further
    # searches. Saves 100s of seconds of useless retries.
    consecutive_failures = 0
    circuit_tripped = False

    def _do_search(query: str) -> list[F95SearchHit]:
        nonlocal client
        if search_fn is not None:
            return search_fn(query)
        if client is None:
            client = build_search_client(cookie_header=cookie_header)
        return search_with_backoff(query, client=client)

    try:
        for idx, (name, hint) in enumerate(entries, start=1):
            if progress_cb:
                progress_cb(idx, len(entries), name)

            # 1. Try local F95Checker first — instant, no network.
            local_id = _try_local_match(name, f95_local)
            if local_id is not None:
                rows.append((
                    "high", name,
                    f"{hint} (already tracked by F95Checker)",
                    f"https://f95zone.to/threads/.{local_id}/",
                ))
                continue

            # 2. Search F95Zone (unless cookies missing or circuit tripped).
            search_term = _save_dir_to_search_term(name)
            if not search_enabled or circuit_tripped:
                reason = (
                    "circuit tripped (rate limited)" if circuit_tripped
                    else "no F95Zone cookies"
                )
                rows.append((
                    "none", name,
                    f"{hint} ({reason}; manual search)",
                    _f95_search_url(search_term),
                ))
                continue
            try:
                hits = _do_search(search_term)
                consecutive_failures = 0  # success resets the breaker
            except Exception as e:
                log.warning("F95Zone search failed for %r: %s", name, e)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    circuit_tripped = True
                    if progress_cb:
                        progress_cb(idx, len(entries),
                                    "*** rate-limit circuit tripped — "
                                    "remaining entries skipped ***")
                rows.append((
                    "none", name,
                    f"{hint} (F95Zone search failed: {type(e).__name__})",
                    _f95_search_url(search_term),
                ))
                continue

            if not hits:
                rows.append((
                    "none", name,
                    f"{hint} (no F95Zone search hits for {search_term!r})",
                    _f95_search_url(search_term),
                ))
                continue

            # Score *every* hit, not just the first — F95Zone's "relevance"
            # ordering puts the right thread near the top but not always at
            # position 0 (e.g. when there are mods/walkthroughs for the same
            # title). Strip bracket tags before scoring so version/dev
            # decorations don't drag the score down.
            best = None
            best_score = -1.0
            for hit in hits[:5]:
                clean = clean_thread_title(hit.title)
                score = fuzz.WRatio(clean, search_term)
                if score > best_score:
                    best, best_score = hit, score

            top = best
            title_score = best_score
            if top is None or title_score < BAD_BELOW:
                rows.append((
                    "none", name,
                    f"{hint} (best hit {(top.title if top else '∅')!r} scored {title_score:.0f} against {search_term!r})",
                    _f95_search_url(search_term),
                ))
                continue
            if title_score >= HIGH_THRESHOLD:
                conf = "high"
            elif title_score >= MEDIUM_THRESHOLD:
                conf = "medium"
            else:
                conf = "low"
            rows.append((
                conf, name,
                f"{hint} (F95Zone match: {top.title!r}, score {title_score:.0f})",
                top.url,
            ))
    finally:
        if client is not None:
            client.close()

    return rows


def _save_credentials_early(
    *,
    paths: VNMasterPaths,
    anthropic_key: str,
    discord_token: str,
    discord_webhook_url: str,
    f95zone_cookies: str | None,
    cfg_paths_section: dict[str, Any],
    cfg_discord_section: dict[str, Any],
    existing_config: dict[str, Any],
) -> None:
    """Persist secrets.toml + config.toml as soon as credentials validate.

    Called from step 4c of the wizard so that an abort in the slow
    candidate-generation step doesn't lose credentials the user already
    entered and we already verified.
    """
    from vnmaster.config import write_private_toml

    paths.config_dir.mkdir(parents=True, exist_ok=True)

    # Preserve any pre-existing config sections we don't manage.
    managed_sections = {"paths", "discord", "anthropic", "schedule",
                        "matching", "magnitude_score"}
    other_sections = {
        k: v for k, v in existing_config.items() if k not in managed_sections
    }
    config_data: dict[str, Any] = {
        "paths": cfg_paths_section,
        "discord": cfg_discord_section,
        "anthropic": existing_config.get("anthropic")
        or {"model": "claude-haiku-4-5", "monthly_budget_usd": 5.0},
        "schedule": existing_config.get("schedule") or {"cron": "0 9 * * SAT", "daily_cron": "0 1 * * *"},
        "matching": existing_config.get("matching") or {"fuzzy_threshold": 90},
        "magnitude_score": existing_config.get("magnitude_score") or {
            "renders": 0.001, "animations": 0.005, "words_per_1k": 0.1,
            "scenes": 0.0, "new_locations": 0.0, "new_characters": 0.0,
            "bugfix_only_penalty": 0.0,
        },
        **other_sections,
    }
    write_private_toml(paths.config_dir / "config.toml", config_data)

    secrets_path = paths.config_dir / "secrets.toml"
    secrets_dict = {
        "discord_bot_token": discord_token,
        "discord_webhook_url": discord_webhook_url,
        "anthropic_api_key": anthropic_key,
    }
    if f95zone_cookies:
        secrets_dict["f95zone_cookies"] = f95zone_cookies
    write_private_toml(secrets_path, secrets_dict)


def _try_local_match(name: str, f95_local: dict[str, int]) -> int | None:
    """Return a F95 thread id only when a HIGH-confidence local match exists."""
    if not f95_local:
        return None
    from rapidfuzz import fuzz, process

    best = process.extractOne(name, list(f95_local.keys()), scorer=fuzz.WRatio)
    if best is None:
        return None
    match_name, score, _ = best
    if score >= HIGH_THRESHOLD:
        return f95_local[match_name]
    return None
