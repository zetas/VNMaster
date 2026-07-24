"""Discover a required game build and optional related downloads."""
from __future__ import annotations

from dataclasses import replace
import re

import httpx

from vnmaster.downloads.f95 import (
    ThreadMetadataError,
    fetch_thread_info,
    resolve_game,
    search_forum_threads,
)
from vnmaster.downloads.models import DownloadPlan, SkippedArtifact, ThreadInfo
from vnmaster.downloads.selector import build_download_plan, is_requested_addon


def prepare_download_plan(
    value: str,
    *,
    client: httpx.Client,
    platform_priority: list[str],
    preferred_hosts: list[str],
    include_addons: bool = True,
    allow_host_fallback: bool = True,
) -> DownloadPlan:
    hit = resolve_game(value, client=client)
    game = fetch_thread_info(hit.thread_id, client=client)

    addons: list[ThreadInfo] = []
    discovery_skips: list[SkippedArtifact] = []
    if include_addons:
        candidates = search_forum_threads(game.title, client=client)
        seen: set[int] = set()
        for candidate in candidates:
            if candidate.thread_id == game.thread_id or candidate.thread_id in seen:
                continue
            seen.add(candidate.thread_id)
            searchable = f"{candidate.title} {candidate.url}"
            if not is_requested_addon(searchable):
                continue
            if not _candidate_refers_to_game(game.title, candidate.url):
                continue
            try:
                info = fetch_thread_info(candidate.thread_id, client=client)
                addons.append(replace(info, title=candidate.title))
            except (httpx.HTTPError, ThreadMetadataError) as exc:
                discovery_skips.append(
                    SkippedArtifact(
                        candidate.title,
                        f"could not load add-on metadata: {type(exc).__name__}",
                    )
                )

    plan = build_download_plan(
        game,
        addons,
        platform_priority=platform_priority,
        preferred_hosts=preferred_hosts,
        allow_host_fallback=allow_host_fallback,
    )
    if discovery_skips:
        plan = replace(plan, skipped=plan.skipped + tuple(discovery_skips))
    return plan


def select_optional_artifacts(
    candidate_plan: DownloadPlan, selected_numbers: tuple[int, ...]
) -> DownloadPlan:
    """Return a plan containing the required build plus selected option numbers."""
    required, *optional = candidate_plan.artifacts
    invalid = [number for number in selected_numbers if not 1 <= number <= len(optional)]
    if invalid:
        raise ValueError(f"Optional download number out of range: {invalid[0]}")
    selected = tuple(optional[number - 1] for number in selected_numbers)
    return replace(candidate_plan, artifacts=(required, *selected))


def _candidate_refers_to_game(game_title: str, candidate_url: str) -> bool:
    game_slug = re.sub(r"[^a-z0-9]+", "-", game_title.casefold()).strip("-")
    return bool(game_slug and game_slug in candidate_url.casefold())
