"""Discover a required game build and optional related downloads."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re

import httpx

from vnmaster.downloads.f95 import (
    ThreadMetadataError,
    fetch_thread_info,
    resolve_game,
    search_forum_threads,
)
from vnmaster.downloads.models import (
    DownloadPlan,
    PartDetection,
    SkippedArtifact,
    ThreadInfo,
)
from vnmaster.downloads.selector import build_download_plan, is_requested_addon


@dataclass(frozen=True)
class ThreadDiscovery:
    game: ThreadInfo
    addons: tuple[ThreadInfo, ...]
    skipped: tuple[SkippedArtifact, ...]


def discover_thread(
    value: str, *, client: httpx.Client, include_addons: bool = True
) -> ThreadDiscovery:
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

    return ThreadDiscovery(game, tuple(addons), tuple(discovery_skips))


def build_plan_from_discovery(
    discovery: ThreadDiscovery,
    *,
    platform_priority: list[str],
    preferred_hosts: list[str],
    allow_host_fallback: bool = True,
    detection: PartDetection | None = None,
    selected_parts: tuple[int, ...] | None = None,
) -> DownloadPlan:
    plan = build_download_plan(
        discovery.game,
        list(discovery.addons),
        platform_priority=platform_priority,
        preferred_hosts=preferred_hosts,
        allow_host_fallback=allow_host_fallback,
        detection=detection,
        selected_parts=selected_parts,
    )
    if discovery.skipped:
        plan = replace(plan, skipped=plan.skipped + discovery.skipped)
    return plan


def prepare_download_plan(
    value: str,
    *,
    client: httpx.Client,
    platform_priority: list[str],
    preferred_hosts: list[str],
    include_addons: bool = True,
    allow_host_fallback: bool = True,
) -> DownloadPlan:
    discovery = discover_thread(value, client=client, include_addons=include_addons)
    return build_plan_from_discovery(
        discovery,
        platform_priority=platform_priority,
        preferred_hosts=preferred_hosts,
        allow_host_fallback=allow_host_fallback,
    )


def select_optional_artifacts(
    candidate_plan: DownloadPlan, selected_numbers: tuple[int, ...]
) -> DownloadPlan:
    """Return required game artifacts plus the selected optional add-ons."""
    required = tuple(a for a in candidate_plan.artifacts if a.kind == "game")
    optional = [a for a in candidate_plan.artifacts if a.kind == "addon"]
    invalid = [n for n in selected_numbers if not 1 <= n <= len(optional)]
    if invalid:
        raise ValueError(f"Optional download number out of range: {invalid[0]}")
    selected = tuple(optional[n - 1] for n in selected_numbers)
    return replace(candidate_plan, artifacts=(*required, *selected))


def _candidate_refers_to_game(game_title: str, candidate_url: str) -> bool:
    game_slug = re.sub(r"[^a-z0-9]+", "-", game_title.casefold()).strip("-")
    return bool(game_slug and game_slug in candidate_url.casefold())
