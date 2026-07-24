"""Deterministic full-build and add-on selection rules."""
from __future__ import annotations

import re

from vnmaster.downloads.models import (
    DownloadGroup,
    DownloadMirror,
    DownloadPlan,
    PlannedArtifact,
    SkippedArtifact,
    ThreadInfo,
)
from vnmaster.magnitude import version_tokens


_ADDON_RE = re.compile(
    r"\b(?:walk\s*-?\s*through|multi\s*-?\s*mod|mod|patch|hotfix|fix|cheat|"
    r"gallery|unlock(?:er)?|translation|uncensor(?:ed)?|save|extras?)\b",
    re.I,
)
_REJECT_GAME_GROUP_RE = re.compile(r"android|compressed|update|patch|hotfix", re.I)
_REJECT_ADDON_GROUP_RE = re.compile(r"android|compressed", re.I)
_OPTIONAL_GROUP_RE = _ADDON_RE


class NoCompatibleDownloadError(RuntimeError):
    pass


def is_requested_addon(title: str) -> bool:
    return bool(_ADDON_RE.search(title))


def addon_matches_game(game: ThreadInfo, addon: ThreadInfo) -> tuple[bool, str]:
    if game.title.casefold() not in addon.title.casefold():
        return False, "title does not identify the selected game"
    game_versions = version_tokens(game.version)
    addon_versions = version_tokens(addon.version)
    if not addon_versions:
        # Unknown versions are unsafe to install automatically, but optional
        # candidates are now explicitly chosen by the user.
        return True, "add-on version is not stated"
    if game_versions and not (game_versions & addon_versions):
        return True, (
            f"reported add-on version {addon.version} may not match game {game.version}"
        )
    return True, ""


def build_download_plan(
    game: ThreadInfo,
    addons: list[ThreadInfo],
    *,
    platform_priority: list[str],
    preferred_hosts: list[str],
    allow_host_fallback: bool = True,
) -> DownloadPlan:
    game_artifact = _select_game_artifact(
        game, platform_priority, preferred_hosts, allow_host_fallback
    )
    selected: list[PlannedArtifact] = [
        game_artifact,
        *_select_embedded_addons(game, game_artifact, preferred_hosts),
    ]
    skipped: list[SkippedArtifact] = []

    for addon in addons:
        compatible, reason = addon_matches_game(game, addon)
        if not compatible:
            skipped.append(SkippedArtifact(addon.title, reason))
            continue
        artifact = _select_addon_artifact(
            addon,
            preferred_hosts,
            warning=reason or None,
        )
        if artifact is None:
            skipped.append(
                SkippedArtifact(addon.title, "no downloadable mirrors found")
            )
            continue
        selected.append(artifact)

    return DownloadPlan(game=game, artifacts=tuple(selected), skipped=tuple(skipped))


def _select_game_artifact(
    game: ThreadInfo,
    platform_priority: list[str],
    preferred_hosts: list[str],
    allow_host_fallback: bool,
) -> PlannedArtifact:
    candidates: list[DownloadMirror] = []
    seen_groups: set[int] = set()
    for platform in platform_priority:
        for group_index, group in enumerate(game.downloads):
            if group_index in seen_groups:
                continue
            if not _group_matches_platform(group.name, platform):
                continue
            if _REJECT_GAME_GROUP_RE.search(group.name):
                continue
            mirrors = _ordered_mirrors(group, preferred_hosts, allow_host_fallback)
            if mirrors:
                seen_groups.add(group_index)
                candidates.extend(
                    DownloadMirror(
                        mirror.name,
                        mirror.locator,
                        platform=platform,
                        group_name=group.name,
                    )
                    for mirror in mirrors
                )
    if candidates:
        mirror, *alternates = candidates
        return PlannedArtifact(
            kind="game",
            title=game.title,
            version=game.version,
            thread_id=game.thread_id,
            thread_url=game.url,
            group_name=mirror.group_name or "Full build",
            platform=mirror.platform,
            host=mirror.name,
            locator=mirror.locator,
            alternate_mirrors=tuple(alternates),
        )
    raise NoCompatibleDownloadError(
        f"No full build for {platform_priority!r} was available from "
        f"{preferred_hosts!r} on {game.title!r}"
    )


def _select_addon_artifact(
    addon: ThreadInfo,
    preferred_hosts: list[str],
    *,
    warning: str | None,
) -> PlannedArtifact | None:
    for group in addon.downloads:
        if _REJECT_ADDON_GROUP_RE.search(group.name):
            continue
        # A forced game host must not hide optional files that only exist as
        # forum attachments or on a different provider.
        mirrors = _ordered_mirrors(group, preferred_hosts, allow_host_fallback=True)
        if not mirrors:
            continue
        mirror, *alternates = mirrors
        return PlannedArtifact(
            kind="addon",
            title=addon.title,
            version=addon.version,
            thread_id=addon.thread_id,
            thread_url=addon.url,
            group_name=group.name,
            platform=None,
            host=mirror.name,
            locator=mirror.locator,
            warning=warning,
            alternate_mirrors=tuple(alternates),
        )
    return None


def _select_embedded_addons(
    game: ThreadInfo,
    game_artifact: PlannedArtifact,
    preferred_hosts: list[str],
) -> list[PlannedArtifact]:
    """Select optional patch/extra groups published in the main game thread."""
    artifacts: list[PlannedArtifact] = []
    for group in game.downloads:
        if group.name == game_artifact.group_name:
            continue
        if not _OPTIONAL_GROUP_RE.search(group.name):
            continue
        mirrors = _ordered_mirrors(group, preferred_hosts, allow_host_fallback=True)
        if not mirrors:
            continue
        mirror, *alternates = mirrors
        artifacts.append(
            PlannedArtifact(
                kind="addon",
                title=f"{game.title} — {group.name}",
                version=game.version,
                thread_id=game.thread_id,
                thread_url=game.url,
                group_name=group.name,
                platform=None,
                host=mirror.name,
                locator=mirror.locator,
                alternate_mirrors=tuple(alternates),
            )
        )
    return artifacts


def _ordered_mirrors(
    group: DownloadGroup,
    preferred_hosts: list[str],
    allow_host_fallback: bool,
) -> tuple[DownloadMirror, ...]:
    preferred: list[DownloadMirror] = []
    for host in preferred_hosts:
        for mirror in group.mirrors:
            if host.casefold() in mirror.name.casefold() and mirror not in preferred:
                preferred.append(mirror)
    if not allow_host_fallback:
        return tuple(preferred)
    return (*preferred, *(mirror for mirror in group.mirrors if mirror not in preferred))


def _group_matches_platform(group_name: str, platform: str) -> bool:
    name = group_name.casefold()
    aliases = {
        "mac": ("mac", "macos", "osx"),
        "windows": ("win", "windows", "pc"),
        "linux": ("linux",),
    }
    return any(alias in name for alias in aliases.get(platform.casefold(), (platform,)))
