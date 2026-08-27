"""Discover a required game build and optional related downloads."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
import re
from urllib.parse import urlsplit

import httpx

from vnmaster.downloads.f95 import (
    ThreadMetadataError,
    extract_thread_id,
    fetch_thread_info,
    resolve_game,
    search_forum_threads,
)
from vnmaster.downloads.models import (
    DownloadMirror,
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


class ProviderPolicyError(ValueError):
    """Raised when enabled providers cannot satisfy a required artifact."""


def discover_thread(
    value: str, *, client: httpx.Client, include_addons: bool = True
) -> ThreadDiscovery:
    hit = resolve_game(value, client=client)
    game = fetch_thread_info(hit.thread_id, client=client)

    addons: list[ThreadInfo] = []
    discovery_skips: list[SkippedArtifact] = []
    if include_addons:
        seen: set[int] = {game.thread_id}
        for group in game.downloads:
            if not is_requested_addon(group.name):
                continue
            for mirror in group.mirrors:
                thread_id = extract_thread_id(mirror.locator)
                if thread_id is None or thread_id in seen:
                    continue
                seen.add(thread_id)
                try:
                    addons.append(fetch_thread_info(thread_id, client=client))
                except (httpx.HTTPError, ThreadMetadataError) as exc:
                    discovery_skips.append(
                        SkippedArtifact(
                            group.name,
                            "could not load linked add-on thread: "
                            f"{type(exc).__name__}",
                        )
                    )

        candidates = search_forum_threads(game.title, client=client)
        for candidate in candidates:
            if candidate.thread_id in seen:
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
    include_addons: bool = True,
) -> DownloadPlan:
    plan = build_download_plan(
        discovery.game,
        list(discovery.addons),
        platform_priority=platform_priority,
        preferred_hosts=preferred_hosts,
        allow_host_fallback=allow_host_fallback,
        detection=detection,
        selected_parts=selected_parts,
        include_addons=include_addons,
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
        include_addons=include_addons,
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


def available_providers(plan: DownloadPlan) -> tuple[str, ...]:
    """Return canonical provider labels in first-seen order."""
    providers: list[str] = []
    seen: set[str] = set()
    for artifact in plan.artifacts:
        for mirror in artifact.mirrors:
            provider = provider_name(mirror)
            key = _provider_key(provider)
            if key not in seen:
                seen.add(key)
                providers.append(provider)
    return tuple(providers)


def apply_provider_policy(
    plan: DownloadPlan,
    allowed_hosts: Iterable[str],
) -> DownloadPlan:
    """Remove disabled mirrors and promote the first enabled fallback.

    This is intentionally an allow-list, unlike the CLI's ``--host`` option,
    which only changes preference order. A plan is rejected when any selected
    artifact has no enabled provider so the UI can explain the problem before
    download or extraction begins.
    """
    allowed = {_provider_key(host) for host in allowed_hosts}
    artifacts = []
    for artifact in plan.artifacts:
        mirrors = tuple(
            replace(mirror, name=provider_name(mirror))
            for mirror in artifact.mirrors
            if _provider_key(provider_name(mirror)) in allowed
        )
        if not mirrors:
            raise ProviderPolicyError(
                f"No enabled download provider is available for {artifact.title!r}."
            )
        primary, *fallbacks = mirrors
        artifacts.append(
            replace(
                artifact,
                host=primary.name,
                locator=primary.locator,
                platform=primary.platform,
                group_name=primary.group_name or artifact.group_name,
                alternate_mirrors=tuple(
                    DownloadMirror(
                        mirror.name,
                        mirror.locator,
                        platform=mirror.platform,
                        group_name=mirror.group_name,
                    )
                    for mirror in fallbacks
                ),
            )
        )
    return replace(plan, artifacts=tuple(artifacts))


def _provider_key(value: str) -> str:
    return " ".join(value.casefold().split())


def provider_name(mirror: DownloadMirror) -> str:
    """Derive a provider from a real or F95-masked locator.

    Forum link captions are frequently descriptions such as ``Part 2+`` or
    ``Download here`` rather than host names. Provider policy must use the
    destination encoded in the locator or deselecting MEGA would not reliably
    remove every MEGA mirror.
    """
    url_match = re.search(r"https?://[^'\"\s)\]]+", mirror.locator, re.I)
    locator = url_match.group(0) if url_match is not None else mirror.locator
    parsed = urlsplit(locator)
    hostname = (parsed.hostname or "").casefold()
    if hostname == "f95zone.to" and "/masked/" in parsed.path.casefold():
        masked = parsed.path.split("/masked/", 1)[1].split("/", 1)[0]
        hostname = masked.casefold()
    if hostname == "attachments.f95zone.to" or (
        hostname == "f95zone.to" and "/attachments/" in parsed.path.casefold()
    ):
        return "F95 ATTACHMENT"
    canonical = {
        "mega.nz": "MEGA",
        "www.mega.nz": "MEGA",
        "pixeldrain.com": "PIXELDRAIN",
        "www.pixeldrain.com": "PIXELDRAIN",
        "gofile.io": "GOFILE",
        "drive.google.com": "GOOGLE DRIVE",
        "drive.proton.me": "PROTONDRIVE",
        "datanodes.to": "DATANODES",
        "vikingfile.com": "VIKINGFILE",
        "mediafire.com": "MEDIAFIRE",
        "www.mediafire.com": "MEDIAFIRE",
        "mixdrop.ag": "MIXDROP",
        "mixdrop.co": "MIXDROP",
        "buzzheavier.com": "BUZZHEAVIER",
        "uploadhaven.com": "UPLOADHAVEN",
        "workupload.com": "WORKUPLOAD",
        "wdho.ru": "WDHO",
    }.get(hostname)
    if canonical is not None:
        return canonical
    if hostname:
        return hostname.removeprefix("www.").upper()
    return mirror.name.strip() or "DOWNLOAD"


def _candidate_refers_to_game(game_title: str, candidate_url: str) -> bool:
    game_slug = re.sub(r"[^a-z0-9]+", "-", game_title.casefold()).strip("-")
    return bool(game_slug and game_slug in candidate_url.casefold())
