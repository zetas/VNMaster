"""Top-level orchestrator for one `vnmaster digest` run.

Owns the data flow but no behavior: each component is injected so the
pipeline can be tested as a unit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from sqlalchemy import Engine, select

from vnmaster.config import Config
from vnmaster.db.engine import session_scope
from vnmaster.db.models import (
    DigestEntry, DigestRun, LibraryGame, Pairing,
)
from vnmaster.db.ro_f95checker import F95CheckerDB, F95CheckerGame
from vnmaster.digest.embeds import build_update_embed
from vnmaster.digest.poster import BotClient, DiscordPoster, Webhook
from vnmaster.digest.select import select_daily_candidates, select_digest_candidates
from vnmaster.logging_setup import get_logger
from vnmaster.magnitude import (
    BaselineResolution,
    changelog_behind_upstream,
    resolve_baseline,
    score_versions,
)
from vnmaster.llm.changelog import ExtractionResult
from vnmaster.matcher import LearnedPairing, MatchResult, match_library
from vnmaster.scanners.types import InstalledGame, PlayHistoryEntry
from vnmaster.status import status_changed as _status_changed

log = get_logger(__name__)

STALE_REFRESH_SECONDS = 24 * 3600


class LLMCache(Protocol):
    def extract_for(
        self,
        thread_id: int,
        raw: str,
        title: str,
    ) -> ExtractionResult: ...


@dataclass
class PipelineDeps:
    engine: Engine
    f95_db: F95CheckerDB
    scan_play_history: Callable[..., list[PlayHistoryEntry]]
    scan_disk: Callable[..., list[InstalledGame]]
    llm_cache: LLMCache
    webhook: Webhook
    bot: BotClient
    config: Config
    now_epoch: int
    channel_id: str
    force: bool = False
    mode: str = "weekly"  # "weekly" or "daily"


async def run_digest_pipeline(deps: PipelineDeps) -> None:
    cfg = deps.config

    # 1. Schema check + read F95Checker
    deps.f95_db.check_schema()
    f95_rows = list(deps.f95_db.iter_all_games())
    stale_warning = _stale_warning(
        deps.f95_db.last_successful_refresh_epoch(), deps.now_epoch
    )

    # 2. Scanners
    play_history = deps.scan_play_history(cfg.paths.renpy_saves_root)
    installed = deps.scan_disk(cfg.paths.games_root)

    # 3. Match
    cached_pairings = _load_pairings(deps.engine)
    match_result = match_library(
        play_history=play_history, installed=installed,
        f95_rows=f95_rows, cached_pairings=cached_pairings,
        fuzzy_threshold=cfg.matching.fuzzy_threshold,
    )

    # 3b. Auto-persist newly learned pairings (name- and version-corroborated).
    _persist_learned_pairings(deps.engine, match_result.learned_pairings, deps.now_epoch)

    # 4. Upsert library_games
    _upsert_library(
        deps.engine, match_result, f95_rows, deps.now_epoch,
        write_status=(deps.mode != "daily"),
    )

    # 5. Select candidates. Daily mode uses the per-version/status watermark
    #    and stays silent when there's nothing new; weekly uses its throttle.
    if deps.mode == "daily":
        candidates = select_daily_candidates(
            engine=deps.engine, f95_rows=f95_rows, now_epoch=deps.now_epoch,
        )
        if not candidates.updates:
            if stale_warning:
                log.warning("daily check: no new updates but F95Checker is stale")
                await deps.webhook.send(content=f"@everyone {stale_warning}")
            else:
                log.info("daily check: no new updates; nothing to post")
            return
    else:
        previous = _previous_run_at(deps.engine)
        candidates = select_digest_candidates(
            engine=deps.engine, previous_digest_run_at=previous,
            now_epoch=deps.now_epoch, max_repeat_weeks=4,
            force_all_behind=deps.force,
        )

    # 6. LLM extract per update
    update_embeds = []
    llm_calls = 0
    llm_cost = 0.0
    for u in candidates.updates:
        result = deps.llm_cache.extract_for(
            thread_id=u.f95_thread_id, raw=u.raw_changelog or "", title=u.game_title
        )
        llm_calls += 1
        llm_cost += result.cost_usd
        resolution = resolve_baseline(result.versions, u.installed_version)
        stale = changelog_behind_upstream(
            result.versions, u.latest_upstream_version
        )
        score = score_versions(resolution.counted, cfg.magnitude_score)
        deltas = _aggregate_deltas(resolution.counted)
        summary = _pick_summary_line(resolution.counted)
        confidence, basis, note = _embed_signals(resolution, stale)
        embed = build_update_embed(
            u, deltas=deltas, magnitude_score=score,
            summary_one_line=summary, confidence=confidence,
            accuracy_basis=basis, no_delta_note=note,
        )
        update_embeds.append((u.f95_thread_id, embed))

    # 7. Post
    poster = DiscordPoster(webhook=deps.webhook, bot_client=deps.bot,
                           channel_id=deps.channel_id)
    if deps.mode == "daily":
        n = len(update_embeds)
        kickoff = (
            f"@everyone New update{'s' if n != 1 else ''} detected "
            f"— {n} game{'s' if n != 1 else ''} behind"
        )
    elif update_embeds:
        kickoff = f"Weekly digest — {len(update_embeds)} library updates"
    else:
        kickoff = "Weekly digest — no tracked games have updates this week."
    if stale_warning:
        ping = "" if "@everyone" in kickoff else "@everyone "
        kickoff = f"{kickoff}\n{ping}{stale_warning}"
    posted = await poster.post(
        kickoff_text=kickoff, update_embeds=update_embeds,
    )

    # 8. Record run + entries. Daily runs are tagged so they never move the
    #    weekly "since last digest" pointer, and they advance the daily
    #    watermark instead of stamping the weekly last-seen throttle.
    notified = {u.f95_thread_id: u for u in candidates.updates} if deps.mode == "daily" else {}
    with session_scope(deps.engine) as s:
        run = DigestRun(
            run_at=deps.now_epoch,
            updates_count=len(update_embeds),
            llm_calls=llm_calls,
            llm_cost_usd=llm_cost,
            kind=deps.mode,
        )
        s.add(run)
        s.flush()
        for message_id, embed_index, kind, thread_id in posted.entries:
            s.add(DigestEntry(
                run_id=run.id, discord_message_id=message_id,
                embed_index=embed_index, kind=kind, f95_thread_id=thread_id,
            ))
            if kind != "update":
                continue
            row = s.execute(
                select(LibraryGame).where(LibraryGame.f95_thread_id == thread_id)
            ).scalar_one_or_none()
            if row is None:
                log.warning(
                    "library_games row missing for thread %d after digest post",
                    thread_id,
                )
                continue
            if deps.mode == "daily":
                notified_update = notified.get(thread_id)
                if notified_update is not None:
                    row.last_daily_notified_version = notified_update.latest_upstream_version
                    row.last_daily_notified_status = notified_update.status
            else:
                row.last_seen_in_digest_at = deps.now_epoch


def _stale_warning(last_refresh_epoch: int | None, now_epoch: int) -> str | None:
    """Warning line when F95Checker hasn't refreshed in over 24h, else None.

    None input means staleness can't be assessed (old schema or never
    refreshed) and suppresses the warning. Age is >= 24h whenever this
    returns text, so the hour/day wording never needs a singular form.
    """
    if last_refresh_epoch is None:
        return None
    age = now_epoch - last_refresh_epoch
    if age <= STALE_REFRESH_SECONDS:
        return None
    if age < 48 * 3600:
        age_text = f"{age // 3600} hours ago"
    else:
        age_text = f"{age // 86400} days ago"
    stamp = datetime.fromtimestamp(last_refresh_epoch).strftime("%b %d %H:%M")
    return (
        f"F95Checker data is stale — last successful refresh was "
        f"{age_text} ({stamp}). Open F95Checker and hit Refresh."
    )


def _persist_learned_pairings(
    engine: Engine,
    learned: list[LearnedPairing],
    now_epoch: int,
) -> None:
    """Upsert auto-learned pairings into the pairings table.

    Confidence is strictly monotonic: an existing row is only updated when the
    new confidence is strictly higher. This means:
    - A manual pairing (confidence=1.0) is never clobbered by an auto match.
    - A name-match (0.95) is not downgraded by a later version-match (0.85).
    """
    if not learned:
        return
    with session_scope(engine) as s:
        for lp in learned:
            existing = s.execute(
                select(Pairing).where(Pairing.f95_thread_id == lp.f95_thread_id)
            ).scalar_one_or_none()

            if existing is None:
                s.add(Pairing(
                    f95_thread_id=lp.f95_thread_id,
                    save_dir_name=lp.save_dir_name,
                    folder_name=lp.folder_name,
                    confidence=lp.confidence,
                    paired_at=now_epoch,
                ))
                log.info(
                    "learned pairing: thread=%d save_dir=%r folder=%r "
                    "method=%s confidence=%.2f",
                    lp.f95_thread_id, lp.save_dir_name, lp.folder_name,
                    lp.method, lp.confidence,
                )
            elif lp.confidence > existing.confidence:
                existing.save_dir_name = lp.save_dir_name
                existing.folder_name = lp.folder_name
                existing.confidence = lp.confidence
                existing.paired_at = now_epoch
                log.info(
                    "upgraded pairing: thread=%d save_dir=%r folder=%r "
                    "method=%s confidence=%.2f (was %.2f)",
                    lp.f95_thread_id, lp.save_dir_name, lp.folder_name,
                    lp.method, lp.confidence, existing.confidence,
                )


def _load_pairings(engine: Engine) -> dict[str, int]:
    out: dict[str, int] = {}
    with session_scope(engine) as s:
        for p in s.execute(select(Pairing)).scalars().all():
            if p.save_dir_name:
                out[p.save_dir_name] = p.f95_thread_id
            if p.folder_name:
                out[p.folder_name] = p.f95_thread_id
    return out


def _upsert_library(
    engine: Engine,
    match_result: MatchResult,
    f95_rows: list[F95CheckerGame],
    now_epoch: int,
    write_status: bool = True,
) -> None:
    f95_by_id = {f.id: f for f in f95_rows}
    matched_ids = {m.f95_thread_id for m in match_result.matches}
    with session_scope(engine) as s:
        for m in match_result.matches:
            f95 = f95_by_id.get(m.f95_thread_id)
            existing = s.execute(
                select(LibraryGame).where(LibraryGame.f95_thread_id == m.f95_thread_id)
            ).scalar_one_or_none()
            data = dict(
                game_title=m.game_title,
                save_dir_name=m.save_dir_name,
                last_played_at=m.last_played_at,
                first_played_at=m.first_played_at,
                save_count=m.save_count,
                total_save_size_bytes=m.total_save_size_bytes,
                install_path=str(m.install_path) if m.install_path else None,
                installed_version=m.installed_version,
                disk_size_bytes=m.disk_size_bytes,
                latest_upstream_version=f95.version if f95 else None,
                upstream_last_updated_at=f95.last_updated if f95 else None,
                upstream_thread_url=(
                    f"https://f95zone.to/threads/.{m.f95_thread_id}/" if f95 else None
                ),
                raw_changelog=f95.changelog if f95 else None,
                tags_json=json.dumps(f95.tags) if f95 else None,
                status=f95.status if f95 else None,
                status_changed=(
                    1 if (f95 is not None and existing is not None
                          and _status_changed(existing.status, f95.status)) else 0
                ),
                developer=f95.developer if f95 else None,
                image_url=f95.image_url if f95 else None,
                updated_at=now_epoch,
            )
            if not write_status and existing is not None:
                data.pop("status", None)
                data.pop("status_changed", None)
            if existing is None:
                s.add(LibraryGame(
                    f95_thread_id=m.f95_thread_id, created_at=now_epoch,
                    last_daily_notified_version=(f95.version if f95 else None),
                    last_daily_notified_status=(f95.status if f95 else None),
                    **data,
                ))
            else:
                for k, v in data.items():
                    setattr(existing, k, v)

        # Also upsert all F95 rows not matched by scanners (known but uninstalled)
        for f95 in f95_rows:
            if f95.id in matched_ids:
                continue
            existing = s.execute(
                select(LibraryGame).where(LibraryGame.f95_thread_id == f95.id)
            ).scalar_one_or_none()
            data = dict(
                game_title=f95.name,
                latest_upstream_version=f95.version,
                upstream_last_updated_at=f95.last_updated,
                upstream_thread_url=f"https://f95zone.to/threads/.{f95.id}/",
                raw_changelog=f95.changelog,
                tags_json=json.dumps(f95.tags),
                status=f95.status,
                status_changed=(
                    1 if (existing is not None
                          and _status_changed(existing.status, f95.status)) else 0
                ),
                developer=f95.developer,
                image_url=f95.image_url,
                updated_at=now_epoch,
            )
            if not write_status and existing is not None:
                data.pop("status", None)
                data.pop("status_changed", None)
            if existing is None:
                s.add(LibraryGame(
                    f95_thread_id=f95.id, created_at=now_epoch,
                    last_daily_notified_version=f95.version,
                    last_daily_notified_status=f95.status,
                    **data,
                ))
            else:
                for k, v in data.items():
                    setattr(existing, k, v)


def _previous_run_at(engine: Engine) -> int:
    with session_scope(engine) as s:
        last = s.execute(
            select(DigestRun)
            .where(DigestRun.kind == "weekly")
            .order_by(DigestRun.run_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return last.run_at if last else 0


def _aggregate_deltas(versions: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("renders", "animations", "words", "scenes",
                "new_locations", "new_characters"):
        total = sum((v.get(key) or 0) for v in versions)
        if total:
            out[key] = total
    return out


def _delta_note(counted: list[dict[str, Any]]) -> str:
    """Plain-English stand-in for the delta field when no metrics were counted.

    Distinguishes "you're current", "just bug fixes", and "real changes the dev
    didn't quantify" — all of which previously rendered as an opaque
    "(no structured deltas extracted)".
    """
    if not counted:
        return "Nothing new in the changelog"
    if all(v.get("bugfix_only") for v in counted):
        return "Bug fixes only"
    return "Changes listed, but no counts given"


def _embed_signals(
    resolution: BaselineResolution, stale: bool
) -> tuple[str, str, str]:
    """Confidence, Accuracy basis, and empty-delta note for the embed.

    When the changelog is behind upstream (notes for the latest release aren't
    posted), the estimate is missing that version — so we override to low
    confidence and say so, rather than implying nothing changed.
    """
    if stale:
        return "low", "notes not posted", "Update available — no changelog notes yet"
    return resolution.confidence, resolution.basis, _delta_note(resolution.counted)


def _pick_summary_line(versions: list[dict[str, Any]]) -> str:
    for v in versions:
        s = v.get("summary_one_line") or ""
        if s:
            return s
    return ""
