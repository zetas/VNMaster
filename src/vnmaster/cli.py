"""CLI entrypoint. Each subcommand is a thin wrapper around a module function."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any, cast

import click
import questionary

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from vnmaster.config import Config
    from vnmaster.downloads.models import DownloadPlan, PlannedArtifact
    from vnmaster.f95_search import F95SearchHit
    from vnmaster.paths import VNMasterPaths


def _load_engine_and_paths(
    config_path: Path | None,
) -> tuple[VNMasterPaths, Config, Engine]:
    """Shared bootstrap: paths + config + engine for read/write commands.

    Also ensures vnmaster.db's schema exists. The wizard doesn't run
    alembic against a fresh DB, so without this every CLI entrypoint
    would crash on "no such table" the first time it ran.
    """
    from vnmaster.config import Config
    from vnmaster.db.engine import create_engine_for, ensure_schema
    from vnmaster.paths import VNMasterPaths

    paths = VNMasterPaths.defaults_for_macos()
    cfg = Config.load(config_path or paths.config_dir / "config.toml")
    engine = create_engine_for(cfg.paths.vnmaster_db)
    ensure_schema(engine)
    return paths, cfg, engine


@click.group()
@click.version_option()
def main() -> None:
    """VNMaster — weekly Discord digest for F95Zone Ren'Py games."""


@main.command()
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Surface every tracked game you're behind on, ignoring the "
         "'already shown recently' throttle. Use for an on-demand full "
         "status report.",
)
@click.option(
    "--daily", "daily", is_flag=True, default=False,
    help="Nightly early-warning mode: alert only on newly-detected updates or "
         "status changes, post nothing when there's nothing new, and leave the "
         "weekly digest's state untouched.",
)
def digest(config_path: Path | None, force: bool, daily: bool) -> None:
    """Run one digest pipeline now and post to Discord."""
    import asyncio
    import time

    import discord
    from anthropic import Anthropic

    from vnmaster.clock import SystemClock
    from vnmaster.config import load_runtime_settings
    from vnmaster.db.engine import create_engine_for, ensure_schema
    from vnmaster.db.ro_f95checker import F95CheckerDB
    from vnmaster.llm.budget import InMemoryBudget
    from vnmaster.llm.cache import CachedExtractor
    from vnmaster.llm.changelog import ChangelogExtractor
    from vnmaster.logging_setup import configure_logging
    from vnmaster.paths import VNMasterPaths
    from vnmaster.pipeline import PipelineDeps, run_digest_pipeline
    from vnmaster.scanners.disk import scan_disk
    from vnmaster.scanners.play_history import scan_play_history

    paths = VNMasterPaths.defaults_for_macos()
    configure_logging(paths.log_dir / "vnmaster.log")
    cfg, secrets = load_runtime_settings(
        config_path or paths.config_dir / "config.toml",
        paths.config_dir / "secrets.toml",
    )

    engine = create_engine_for(cfg.paths.vnmaster_db)
    ensure_schema(engine)
    f95_db = F95CheckerDB.open(cfg.paths.f95checker_db)
    anthropic_client = Anthropic(api_key=secrets.anthropic_api_key)
    budget = InMemoryBudget(cap_usd=cfg.anthropic.monthly_budget_usd)
    extractor = ChangelogExtractor(
        client=anthropic_client, model=cfg.anthropic.model, budget_tracker=budget
    )
    cache = CachedExtractor(inner=extractor, engine=engine, clock=lambda: int(time.time()))

    webhook = discord.SyncWebhook.from_url(secrets.required_discord_webhook_url)

    class _WebhookAdapter:
        """Wraps discord.SyncWebhook in the async-shaped interface our
        DiscordPoster expects, converting plain-dict embeds to
        discord.Embed instances on the way through."""

        def __init__(self, w: discord.SyncWebhook) -> None:
            self._w = w

        async def send(
            self,
            content: str | None = None,
            embeds: list[Any] | None = None,
        ) -> Any:
            kwargs: dict[str, Any] = {
                "wait": True,
                "allowed_mentions": discord.AllowedMentions(everyone=True),
            }
            if content is not None:
                kwargs["content"] = content
            if embeds is not None:
                kwargs["embeds"] = [
                    discord.Embed.from_dict(e) if isinstance(e, dict) else e
                    for e in embeds
                ]
            return self._w.send(**kwargs)

    bot = discord.Client(intents=discord.Intents.none())

    class _BotAdapter:
        """Exposes the (channel_id, message_id, emoji) → add_reaction shape
        our DiscordPoster expects, by fetching channel + message first."""

        def __init__(self, b: discord.Client) -> None:
            self._b = b
            self._channel_cache: discord.TextChannel | discord.Thread | None = None

        async def add_reaction(
            self,
            channel_id: str,
            message_id: str,
            emoji: str,
        ) -> None:
            if self._channel_cache is None:
                channel = await self._b.fetch_channel(int(channel_id))
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    raise RuntimeError(
                        f"Discord channel {channel_id} cannot contain messages"
                    )
                self._channel_cache = channel
            msg = await self._channel_cache.fetch_message(int(message_id))
            await msg.add_reaction(emoji)

    deps = PipelineDeps(
        engine=engine, f95_db=f95_db, scan_play_history=scan_play_history,
        scan_disk=scan_disk,
        llm_cache=cache, webhook=_WebhookAdapter(webhook), bot=_BotAdapter(bot),
        config=cfg, now_epoch=SystemClock().now_epoch(),
        channel_id=cfg.discord.channel_id, force=force,
        mode="daily" if daily else "weekly",
    )

    async def main_async() -> None:
        await bot.login(secrets.discord_bot_token)
        try:
            await run_digest_pipeline(deps)
        finally:
            await bot.close()

    asyncio.run(main_async())
    click.echo("digest run complete")


@main.command()
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
def bot(config_path: Path | None) -> None:
    """Run the Discord reaction bot daemon in the foreground."""
    from vnmaster.bot.client import run_bot
    from vnmaster.config import Secrets
    from vnmaster.logging_setup import configure_logging

    paths, cfg, engine = _load_engine_and_paths(config_path)
    configure_logging(paths.log_dir / "vnmaster.log")
    secrets = Secrets.load(paths.config_dir / "secrets.toml")

    resolved_config_path = config_path or (paths.config_dir / "config.toml")

    click.echo(
        f"Starting VNMaster bot for guild {cfg.discord.guild_id}, "
        f"channel {cfg.discord.channel_id}. Ctrl-C to stop."
    )
    run_bot(
        token=secrets.discord_bot_token,
        engine=engine,
        channel_id=int(cfg.discord.channel_id),
        guild_id=int(cfg.discord.guild_id),
        config_path=resolved_config_path,
    )


@main.command()
def init() -> None:
    """Interactive first-run setup wizard."""
    from vnmaster.init_wizard import run_wizard
    from vnmaster.paths import VNMasterPaths

    run_wizard(VNMasterPaths.defaults_for_macos())


@main.command()
@click.argument("name")
@click.argument("f95_url")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
def pair(name: str, f95_url: str, config_path: Path | None) -> None:
    """Manually pair a save dir / folder name to an F95 thread URL."""
    import time

    from vnmaster.bot.slash import InvalidUrlError, cmd_pair

    _paths, _cfg, engine = _load_engine_and_paths(config_path)
    try:
        result = cmd_pair(
            engine=engine, name=name, f95_url=f95_url, now_epoch=int(time.time())
        )
    except InvalidUrlError as e:
        raise click.ClickException(str(e)) from e
    click.echo(result)


@main.command()
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
def status(config_path: Path | None) -> None:
    """Print last digest run, llm cost MTD, unmatched count."""
    from vnmaster.bot.slash import cmd_status

    _paths, _cfg, engine = _load_engine_and_paths(config_path)
    click.echo(cmd_status(engine=engine))


@main.command()
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
def pairings(config_path: Path | None) -> None:
    """List all save-folder → F95 thread pairings."""
    from vnmaster.bot.slash import cmd_pairings_list

    _paths, _cfg, engine = _load_engine_and_paths(config_path)
    click.echo(cmd_pairings_list(engine=engine))


@main.command()
@click.argument("name")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
def unpair(name: str, config_path: Path | None) -> None:
    """Remove a pairing by save dir name, folder name, or numeric thread id."""
    from vnmaster.bot.slash import NoSuchPairingError, cmd_unpair

    _paths, _cfg, engine = _load_engine_and_paths(config_path)
    try:
        result = cmd_unpair(engine=engine, name=name)
    except NoSuchPairingError:
        raise click.ClickException(f"No pairing found for {name!r}") from None
    click.echo(result)


@main.command(name="install-scheduler")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
def install_scheduler(config_path: Path | None) -> None:
    """Render and install the launchd plists for the weekly digest + bot.

    Standalone version of the wizard's final step. Writes
    ~/Library/LaunchAgents/dev.vnmaster.{weekly,bot}.plist and prints the
    launchctl bootstrap commands to load them.
    """
    import shutil

    from vnmaster.launchd import (
        install_daily_plist, install_plists, render_bot_plist,
        render_daily_plist, render_weekly_plist,
    )

    paths, cfg, _engine = _load_engine_and_paths(config_path)
    bin_path = Path(shutil.which("vnmaster") or "vnmaster")
    paths.log_dir.mkdir(parents=True, exist_ok=True)

    weekly = render_weekly_plist(
        bin_path=bin_path, log_dir=paths.log_dir, cron=cfg.schedule.cron
    )
    bot = render_bot_plist(bin_path=bin_path, log_dir=paths.log_dir)
    launchagents = Path.home() / "Library" / "LaunchAgents"
    weekly_path, bot_path = install_plists(
        weekly_text=weekly, bot_text=bot, launchagents_dir=launchagents
    )
    daily = render_daily_plist(
        bin_path=bin_path, log_dir=paths.log_dir, cron=cfg.schedule.daily_cron
    )
    daily_path = install_daily_plist(daily_text=daily, launchagents_dir=launchagents)
    click.echo(f"Installed:\n  {weekly_path}\n  {daily_path}\n  {bot_path}\n")
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


@main.command(name="suggest-pairs")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
@click.option(
    "--apply", is_flag=True, default=False,
    help="Write corroborated pairings (fuzzy>=70 + version match) to the DB.",
)
def suggest_pairs(config_path: Path | None, apply: bool) -> None:
    """Review unmatched save dirs and suggest (or apply) pairings.

    For each save dir that could not be matched by name alone, shows the
    best F95 candidate with its fuzzy score, whether the played version
    corroborates the match, and a suggestion label:

    \b
      corroborated  fuzzy>=70 AND version tokens intersect (auto-applied with --apply)
      review        fuzzy 70-89, no version corroboration — paste URL into `vnmaster pair`
      weak          fuzzy <70 — unlikely match, listed for awareness

    Without --apply the table is printed but nothing is written. With --apply,
    corroborated rows are upserted (confidence 0.85) respecting the no-clobber
    rule: an existing pairing with higher confidence is never downgraded.
    """
    import time

    from rapidfuzz import fuzz, process

    from vnmaster.db.ro_f95checker import F95CheckerDB
    from vnmaster.matcher import (
        LearnedPairing, match_library, version_tokens,
    )
    from vnmaster.pipeline import _load_pairings, _persist_learned_pairings
    from vnmaster.scanners.play_history import scan_play_history

    paths, cfg, engine = _load_engine_and_paths(config_path)
    f95_db = F95CheckerDB.open(cfg.paths.f95checker_db)
    f95_db.check_schema()
    f95_rows = list(f95_db.iter_all_games())

    play_history = scan_play_history(cfg.paths.renpy_saves_root)
    cached_pairings = _load_pairings(engine)

    # Run matching in name-only mode (disable corroboration) so that the
    # "unmatched" list includes all saves that fuzzy-name alone can't resolve.
    # The table below then shows which of those could be corroborated by version.
    match_result = match_library(
        play_history=play_history,
        installed=[],
        f95_rows=f95_rows,
        cached_pairings=cached_pairings,
        fuzzy_threshold=cfg.matching.fuzzy_threshold,
        corroboration_floor=101,  # effectively disabled — name-only pass
    )

    unmatched = match_result.unmatched_play_history
    if not unmatched:
        click.echo("All save dirs are matched. Nothing to suggest.")
        return

    f95_names = [f.name for f in f95_rows]
    f95_by_name = {f.name: f for f in f95_rows}
    corroboration_floor = 70

    # Each row: (save_dir, played_version, candidate_name, thread_id,
    #             fuzzy_score, version_match, suggestion)
    rows: list[tuple[str, str, str, int, float, bool, str]] = []
    for save in unmatched:
        if not f95_names:
            break
        from vnmaster.matcher import _clean_for_match
        cleaned = _clean_for_match(save.save_dir_name)
        best = process.extractOne(cleaned, f95_names, scorer=fuzz.WRatio)
        if best is None:
            continue
        candidate_name, score, _ = best
        candidate = f95_by_name[candidate_name]
        save_vtokens = version_tokens(save.last_played_version)
        f95_vtokens = version_tokens(candidate.version)
        ver_match = bool(save_vtokens and (save_vtokens & f95_vtokens))
        if score >= corroboration_floor and ver_match:
            suggestion = "corroborated"
        elif score >= corroboration_floor:
            suggestion = "review"
        else:
            suggestion = "weak"
        rows.append((
            save.save_dir_name,
            save.last_played_version or "",
            candidate_name,
            candidate.id,
            score,
            ver_match,
            suggestion,
        ))

    # Sort by fuzzy score descending.
    rows.sort(key=lambda r: r[4], reverse=True)

    # Print table.
    header = (
        f"{'save_dir':<40} {'played_ver':<14} {'best_candidate':<32} "
        f"{'fuzzy':>6} {'ver':>5} suggestion"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for save_dir, pver, cand, tid, score, vmat, sug in rows:
        click.echo(
            f"{save_dir:<40} {pver:<14} {cand:<32} "
            f"{score:>6.1f} {'yes' if vmat else 'no':>5} {sug}"
        )

    corroborated = [r for r in rows if r[6] == "corroborated"]
    review_and_weak = [r for r in rows if r[6] != "corroborated"]

    click.echo("")
    if apply:
        if corroborated:
            learned = [
                LearnedPairing(
                    f95_thread_id=r[3],
                    save_dir_name=r[0],
                    folder_name=None,
                    confidence=0.85,
                    method="version",
                )
                for r in corroborated
            ]
            _persist_learned_pairings(engine, learned, now_epoch=int(time.time()))
            click.echo(f"Applied {len(learned)} corroborated pairing(s).")
        else:
            click.echo("No corroborated pairings to apply.")
        if review_and_weak:
            click.echo("\nRemaining for manual pairing (`vnmaster pair <name> <url>`):")
            for save_dir, _, cand, tid, score, _, sug in review_and_weak:
                url = f"https://f95zone.to/threads/.{tid}/"
                click.echo(f"  {save_dir!r}  [{sug}]  best guess: {cand!r}  {url}")
    else:
        click.echo(
            f"Found {len(corroborated)} corroborated, "
            f"{len(review_and_weak)} review/weak suggestions."
        )
        click.echo(
            "Run with --apply to write the corroborated ones to the DB, "
            "or use `vnmaster pair <name> <url>` for the rest."
        )
        for save_dir, _, cand, tid, score, _, sug in review_and_weak:
            url = f"https://f95zone.to/threads/.{tid}/"
            click.echo(f"  review/weak: {save_dir!r}  →  {url}")


@main.command(name="debug-search")
@click.argument("query")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
@click.option(
    "--save", "save_path", type=click.Path(path_type=Path), default=None,
    help="Write the raw JSON response to this path for inspection.",
)
def debug_search(query: str, config_path: Path | None, save_path: Path | None) -> None:
    """Diagnostic: run one F95Zone latest_alpha search and report the
    response — endpoint URL, status, JSON shape, and parsed hits.
    """
    import json

    from vnmaster.config import Secrets
    from vnmaster.f95_search import (
        SEARCH_ENDPOINT, build_search_client, search_f95zone,
    )

    paths, _cfg, _engine = _load_engine_and_paths(config_path)
    secrets = Secrets.load(paths.config_dir / "secrets.toml")
    cookie_header = secrets.f95zone_cookies

    if cookie_header:
        click.echo(f"Using Cookie header (len {len(cookie_header)})")
    else:
        click.echo("No F95Zone cookies in secrets.toml (search may still work).")

    with build_search_client(cookie_header=cookie_header) as client:
        click.echo(f"\nGET {SEARCH_ENDPOINT}?cmd=list&cat=games&search={query}&page=1")
        resp = client.get(
            SEARCH_ENDPOINT,
            params={"cmd": "list", "cat": "games", "search": query, "page": "1"},
        )
        click.echo(f"Status: {resp.status_code}")
        click.echo(f"Content-Type: {resp.headers.get('content-type', '?')!r}")
        click.echo(f"Length: {len(resp.text)} chars")

        try:
            body = resp.json()
        except Exception as e:
            click.echo(f"JSON decode failed: {e}")
            click.echo(f"Raw body[:300]: {resp.text[:300]!r}")
            return

        click.echo(f"\nTop-level keys: {list(body.keys())}")
        click.echo(f"status: {body.get('status')!r}")
        msg = body.get("msg") or {}
        if isinstance(msg, dict):
            click.echo(f"msg keys: {list(msg.keys())}")
            count = msg.get("count")
            data = msg.get("data") or []
            click.echo(f"count: {count}, data entries: {len(data)}")
            click.echo("\nFirst 5 entries:")
            for entry in data[:5]:
                click.echo(
                    f"  #{entry.get('thread_id')} {entry.get('title')!r} "
                    f"v{entry.get('version')} by {entry.get('creator')} "
                    f"(likes={entry.get('likes')})"
                )

        if resp.status_code == 429:
            click.echo(
                "\nF95Zone is rate-limiting this IP. Wait 5-30 minutes\n"
                "and try again, or use a different network."
            )
            if save_path:
                save_path.write_text(json.dumps(body, indent=2))
            return

        # Run through the actual search function too, for parity.
        try:
            hits = search_f95zone(query, client=client)
        except Exception as e:
            click.echo(f"\nsearch_f95zone() raised {type(e).__name__}: {e}")
            return
        click.echo(f"\nsearch_f95zone() returned {len(hits)} hit(s):")
        for h in hits[:5]:
            click.echo(f"  #{h.thread_id} {h.title!r} (v{h.version} by {h.creator})")

        if save_path:
            save_path.write_text(json.dumps(body, indent=2))
            click.echo(f"\nRaw JSON saved to {save_path}")


@main.command()
@click.argument("game")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
@click.option(
    "--dest", "destination", type=click.Path(path_type=Path), default=None,
    help="Destination root (default: downloads.destination from config).",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Resolve and display the plan without downloading anything.",
)
@click.option(
    "--yes", "assume_yes", is_flag=True, default=False,
    help="Skip the final plan confirmation (CAPTCHA links may still prompt).",
)
@click.option(
    "--no-addons", is_flag=True, default=False,
    help="Download only the full game build.",
)
@click.option(
    "--host", "preferred_host",
    type=str,
    default=None,
    help="Try this host first within each platform while retaining all fallbacks.",
)
@click.option(
    "--download-url", "download_urls", multiple=True,
    help="Resolved host URL, repeated in the same order as planned artifacts.",
)
@click.option(
    "--force-incompatible-addons",
    is_flag=True,
    default=False,
    help="Allow selected add-ons whose reported version differs from the game.",
)
def fetch(
    game: str,
    config_path: Path | None,
    destination: Path | None,
    dry_run: bool,
    assume_yes: bool,
    no_addons: bool,
    preferred_host: str | None,
    download_urls: tuple[str, ...],
    force_incompatible_addons: bool,
) -> None:
    """Download and extract the latest full build for GAME.

    GAME may be a title, F95 thread ID, or F95 thread URL. The full build is
    required; discovered patches, mods, walkthroughs, and other extras are
    offered as an interactive opt-in list.
    """
    import httpx

    from vnmaster.config import Secrets
    from vnmaster.downloads.downloader import is_url_for_host
    from vnmaster.downloads.f95 import AmbiguousGameError, resolve_redacted_locator
    from vnmaster.downloads.mega import find_mega_get
    from vnmaster.downloads.models import ResolvedDownload
    from vnmaster.downloads.service import execute_download_plan_detailed
    from vnmaster.downloads.state import save_install_state
    from vnmaster.downloads.workflow import (
        prepare_download_plan,
        select_optional_artifacts,
    )
    from vnmaster.f95_search import build_search_client

    paths, cfg, engine = _load_engine_and_paths(config_path)
    secrets = Secrets.load(paths.config_dir / "secrets.toml")
    destination = destination or cfg.downloads.destination

    try:
        with build_search_client(cookie_header=secrets.f95zone_cookies) as client:
            plan_input = game
            try:
                candidate_plan = prepare_download_plan(
                    plan_input,
                    client=client,
                    platform_priority=cfg.downloads.platform_priority,
                    preferred_hosts=(
                        [preferred_host]
                        if preferred_host
                        else cfg.downloads.preferred_hosts
                    ),
                    include_addons=not no_addons,
                    allow_host_fallback=True,
                )
            except AmbiguousGameError as exc:
                selected_hit = _prompt_game_resolution(exc.hits)
                plan_input = str(selected_hit.thread_id)
                candidate_plan = prepare_download_plan(
                    plan_input,
                    client=client,
                    platform_priority=cfg.downloads.platform_priority,
                    preferred_hosts=(
                        [preferred_host]
                        if preferred_host
                        else cfg.downloads.preferred_hosts
                    ),
                    include_addons=not no_addons,
                    allow_host_fallback=True,
                )
            _print_download_candidates(candidate_plan, destination)
            if dry_run:
                return

            plan = candidate_plan
            optional_artifacts = candidate_plan.artifacts[1:]
            if optional_artifacts:
                selected_numbers = _prompt_optional_selection(optional_artifacts)
                plan = select_optional_artifacts(candidate_plan, selected_numbers)
                plan = _guard_incompatible_addons(
                    plan,
                    force=force_incompatible_addons,
                    assume_yes=assume_yes,
                )
                _print_selected_artifacts(plan)

            if any(
                "mega" in mirror.name.casefold()
                for artifact in plan.artifacts
                for mirror in artifact.mirrors
            ):
                try:
                    mega_get = find_mega_get()
                except RuntimeError as exc:
                    click.echo(f"MEGAcmd unavailable; MEGA mirrors may fail: {exc}")
                else:
                    click.echo(f"MEGAcmd: {mega_get}")
            if not assume_yes and not click.confirm("Download and extract this plan?"):
                click.echo("Cancelled.")
                return

            supplied = iter(download_urls)
            resolved_downloads: list[tuple[ResolvedDownload, ...]] = []
            for artifact in plan.artifacts:
                supplied_url = next(supplied, None)
                if supplied_url is not None:
                    supplied_mirror = next(
                        (
                            mirror
                            for mirror in artifact.mirrors
                            if is_url_for_host(mirror.name, supplied_url)
                        ),
                        None,
                    )
                    if supplied_mirror is None:
                        raise click.ClickException(
                            f"Invalid --download-url for {artifact.title!r}"
                        )
                    resolved_downloads.append(
                        (
                            ResolvedDownload(
                                supplied_mirror.name,
                                supplied_mirror.locator,
                                supplied_url,
                                platform=supplied_mirror.platform,
                                group_name=supplied_mirror.group_name,
                            ),
                        )
                    )
                    continue

                candidates: list[ResolvedDownload] = []
                protected: list[
                    tuple[str, str, str, str | None, str | None]
                ] = []
                for mirror in artifact.mirrors:
                    try:
                        locator = resolve_redacted_locator(
                            mirror.locator,
                            thread_url=artifact.thread_url,
                            client=client,
                        )
                    except (RuntimeError, httpx.HTTPError) as exc:
                        click.echo(
                            f"Could not resolve {mirror.name} for {artifact.title!r}: "
                            f"{' '.join(str(exc).split()) or type(exc).__name__}"
                        )
                        continue
                    if is_url_for_host(mirror.name, locator):
                        candidates.append(
                            ResolvedDownload(
                                mirror.name,
                                mirror.locator,
                                locator,
                                platform=mirror.platform,
                                group_name=mirror.group_name,
                            )
                        )
                    elif "/masked/" in locator:
                        protected.append(
                            (
                                mirror.name,
                                mirror.locator,
                                locator,
                                mirror.platform,
                                mirror.group_name,
                            )
                        )
                    else:
                        click.echo(
                            f"Skipping {mirror.name} for {artifact.title!r}: "
                            "the link did not resolve to a supported URL."
                        )

                if not candidates and protected:
                    host, source_locator, protected_url, platform, group_name = protected[0]
                    click.echo(
                        f"\nF95 requires a browser CAPTCHA for {artifact.title!r}."
                    )
                    click.echo(
                        f"Opening the protected link; continue to {host}, "
                        "then copy its URL."
                    )
                    click.launch(protected_url)
                    pasted = click.prompt(
                        f"Paste the resulting {host} URL", hide_input=True
                    )
                    if not is_url_for_host(host, pasted):
                        raise click.ClickException(
                            f"The pasted value is not an HTTPS {host} URL"
                        )
                    candidates.append(
                        ResolvedDownload(
                            host,
                            source_locator,
                            pasted,
                            platform=platform,
                            group_name=group_name,
                        )
                    )

                if not candidates:
                    raise click.ClickException(
                        f"No mirrors for {artifact.title!r} could be resolved."
                    )
                resolved_downloads.append(tuple(candidates))

        execution = execute_download_plan_detailed(
            plan,
            resolved_downloads=resolved_downloads,
            destination_root=destination.expanduser(),
            urm_mods_dir=cfg.paths.games_root / "Mods",
            reporter=click.echo,
        )
        state = save_install_state(engine, execution, reporter=click.echo)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Recorded install state: #{state.id}")
    click.echo(f"Ready: {execution.final_dir}")


@main.command()
@click.argument("game")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
@click.option(
    "--yes", "assume_yes", is_flag=True, default=False,
    help="Rebuild without the confirmation prompt.",
)
@click.option(
    "--no-backup", is_flag=True, default=False,
    help="Discard the current game after a successful rebuild instead of backing it up.",
)
def rebuild(
    game: str,
    config_path: Path | None,
    assume_yes: bool,
    no_backup: bool,
) -> None:
    """Rebuild a recorded GAME selected by title, thread ID, or install path."""
    from vnmaster.downloads.rebuild import rebuild_install
    from vnmaster.downloads.state import mark_rebuilt, resolve_install_state

    _paths, cfg, engine = _load_engine_and_paths(config_path)
    try:
        state = resolve_install_state(engine, game)
        click.echo(
            f"Rebuild: {state.game_title} · {state.version or 'unknown version'}"
        )
        click.echo(f"Install: {state.install_path}")
        click.echo(f"Preserved payloads: {len(state.archive_hashes)}")
        if not assume_yes and not click.confirm(
            "Re-extract and reapply recorded add-ons and URM?"
        ):
            click.echo("Cancelled.")
            return
        result = rebuild_install(
            state,
            urm_mods_dir=cfg.paths.games_root / "Mods",
            keep_backup=not no_backup,
            reporter=click.echo,
        )
        mark_rebuilt(
            engine,
            state,
            verification_checks=result.verification_checks,
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if result.backup_path is not None:
        click.echo(f"Backup: {result.backup_path / 'game'}")
    click.echo(f"Rebuilt: {result.install_path / 'game'}")


@main.command("installs")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    default=None, help="Path to config.toml",
)
def installs(config_path: Path | None) -> None:
    """List game installations recorded for rebuild and verification."""
    from vnmaster.downloads.state import list_install_states

    _paths, _cfg, engine = _load_engine_and_paths(config_path)
    states = list_install_states(engine)
    if not states:
        click.echo("No recorded game installs.")
        return
    for state in states:
        rebuilt = (
            f" · rebuilt {state.last_rebuilt_at}"
            if state.last_rebuilt_at is not None
            else ""
        )
        click.echo(
            f"#{state.f95_thread_id} {state.game_title} "
            f"{state.version or 'unknown'} · {state.install_path}{rebuilt}"
        )


def _print_download_candidates(plan: DownloadPlan, destination: Path) -> None:
    click.echo(
        f"Resolved: {plan.game.title} · {plan.game.version or 'unknown version'} "
        f"· thread #{plan.game.thread_id}"
    )
    click.echo(f"Destination: {destination.expanduser()}")
    required, *optional = plan.artifacts
    click.echo("Required download:")
    _print_artifact(required, prefix="  ")
    if optional:
        click.echo("Optional downloads found:")
        for index, artifact in enumerate(optional, start=1):
            _print_artifact(artifact, prefix=f"  {index}. ")
    else:
        click.echo("Optional downloads found: none")
    if plan.skipped:
        click.echo("Unavailable optional downloads:")
        for skipped in plan.skipped:
            click.echo(f"  - {skipped.title}: {skipped.reason}")


def _print_selected_artifacts(plan: DownloadPlan) -> None:
    click.echo("Selected download plan:")
    for index, artifact in enumerate(plan.artifacts, start=1):
        _print_artifact(artifact, prefix=f"  {index}. ")


def _print_artifact(artifact: PlannedArtifact, *, prefix: str) -> None:
    from vnmaster.downloads.addon_installer import should_install_addon

    platform = f" · {artifact.platform}" if artifact.platform else ""
    version = f" · {artifact.version}" if artifact.kind == "addon" and artifact.version else ""
    fallback_count = len(artifact.alternate_mirrors)
    fallbacks = (
        f" · {fallback_count} fallback{'s' if fallback_count != 1 else ''}"
        if fallback_count
        else ""
    )
    action = ""
    if artifact.kind == "addon":
        action = " · installs into game" if should_install_addon(artifact) else " · kept separate"
    click.echo(
        f"{prefix}{artifact.kind}: {artifact.title} "
        f"[{artifact.group_name} · {artifact.host}{fallbacks}{platform}{version}"
        f"{action}]"
    )
    if artifact.warning:
        click.echo(f"      Warning: {artifact.warning}")


def _prompt_optional_selection(
    optional_artifacts: tuple[PlannedArtifact, ...],
) -> tuple[int, ...]:
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _prompt_optional_selection_menu(optional_artifacts)

    return _prompt_optional_selection_fallback(len(optional_artifacts))


def _prompt_optional_selection_menu(
    optional_artifacts: tuple[PlannedArtifact, ...],
) -> tuple[int, ...]:
    choices = [
        questionary.Choice(
            title=_optional_choice_label(artifact),
            value=index,
        )
        for index, artifact in enumerate(optional_artifacts, start=1)
    ]
    answer = questionary.checkbox(
        "Select optional downloads",
        choices=choices,
        instruction="(↑/↓ move • Space toggle • Enter continue)",
        pointer="›",
        use_arrow_keys=True,
        use_jk_keys=True,
    ).ask()
    if answer is None:
        raise click.Abort()
    return tuple(sorted(int(number) for number in answer))


def _optional_choice_label(artifact: PlannedArtifact) -> str:
    from vnmaster.downloads.addon_installer import should_install_addon

    version = f" · {artifact.version}" if artifact.version else ""
    warning = " ⚠" if artifact.warning else ""
    action = " · install" if should_install_addon(artifact) else " · keep separate"
    return f"{artifact.title} · {artifact.host}{version}{action}{warning}"


def _guard_incompatible_addons(
    plan: DownloadPlan,
    *,
    force: bool,
    assume_yes: bool,
) -> DownloadPlan:
    from vnmaster.downloads.models import DownloadPlan

    incompatible = [
        artifact
        for artifact in plan.artifacts[1:]
        if artifact.warning
        and "may not match game" in artifact.warning.casefold()
    ]
    if not incompatible or force:
        return plan
    titles = ", ".join(
        f"{artifact.title} ({artifact.version or 'unknown'})"
        for artifact in incompatible
    )
    if assume_yes or not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise click.ClickException(
            "Selected add-on version does not match the game: "
            f"{titles}. Re-run with --force-incompatible-addons to install it."
        )
    if click.confirm(
        f"These add-ons may be incompatible: {titles}. Install them anyway?"
    ):
        return plan
    rejected = set(incompatible)
    click.echo("Skipped incompatible add-ons.")
    return DownloadPlan(
        plan.game,
        tuple(
            artifact for artifact in plan.artifacts if artifact not in rejected
        ),
        plan.skipped,
    )


def _prompt_game_resolution(hits: list[F95SearchHit]) -> F95SearchHit:
    if not hits:
        raise click.ClickException("No game candidates were available")
    if sys.stdin.isatty() and sys.stdout.isatty():
        choices = [
            questionary.Choice(title=_game_choice_label(hit), value=index)
            for index, hit in enumerate(hits)
        ]
        answer = questionary.select(
            "Select the intended F95 game",
            choices=choices,
            instruction="(↑/↓ move • Enter select)",
            pointer="›",
            use_arrow_keys=True,
            use_jk_keys=True,
        ).ask()
        if answer is None:
            raise click.Abort()
        return hits[int(answer)]

    click.echo("Multiple exact or similarly ranked games were found:")
    for index, hit in enumerate(hits, start=1):
        click.echo(f"  {index}. {_game_choice_label(hit)}")
    while True:
        selected = cast(int, click.prompt("Choose game", type=int))
        if 1 <= selected <= len(hits):
            return hits[selected - 1]
        click.echo(f"Choose a number from 1 to {len(hits)}.")


def _game_choice_label(hit: F95SearchHit) -> str:
    version = hit.version or "unknown version"
    creator = f" · {hit.creator}" if hit.creator else ""
    return f"{hit.title} · {version}{creator} · thread #{hit.thread_id}"


def _prompt_optional_selection_fallback(optional_count: int) -> tuple[int, ...]:
    while True:
        raw = click.prompt(
            "Choose optional downloads (numbers/ranges, 'all', or Enter for none)",
            default="",
            show_default=False,
        )
        try:
            return _parse_optional_selection(raw, optional_count)
        except ValueError as exc:
            click.echo(f"Invalid selection: {exc}")


def _parse_optional_selection(raw: str, optional_count: int) -> tuple[int, ...]:
    value = raw.strip().casefold()
    if value in {"", "none"}:
        return ()
    if value == "all":
        return tuple(range(1, optional_count + 1))

    selected: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            raise ValueError("empty item")
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"expected a numeric range, got {token!r}")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"range starts after it ends: {token!r}")
            selected.update(range(start, end + 1))
        elif token.isdigit():
            selected.add(int(token))
        else:
            raise ValueError(f"expected a number, got {token!r}")

    invalid = sorted(number for number in selected if not 1 <= number <= optional_count)
    if invalid:
        raise ValueError(f"number {invalid[0]} is outside 1-{optional_count}")
    return tuple(sorted(selected))
