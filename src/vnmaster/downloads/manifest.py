"""Strict forum-download manifest schema and deterministic link registry."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
import re
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from vnmaster.downloads.models import (
    DetectedPart,
    DownloadMirror,
    DownloadPlan,
    PartDetection,
    PlannedArtifact,
    SkippedArtifact,
    ThreadInfo,
)
from vnmaster.downloads.f95 import extract_thread_id, is_likely_download_locator
from vnmaster.downloads.selector import addon_matches_game, select_addon_artifact
from vnmaster.magnitude import version_tokens


MANIFEST_SCHEMA_VERSION = 1
# Add-ons whose artifact or chosen-variant confidence falls below this are left out of
# the plan. The forum parser prompt quotes the same number so the model knows the cut.
ADDON_CONFIDENCE_FLOOR = 0.70
ShortText = Annotated[str, Field(max_length=200)]
BoundedText = Annotated[str, Field(max_length=500)]
_PART_RE = re.compile(r"\b(part|pt|chapter|ch|episode|ep|volume|vol)\s*[.#-]?(\d+)\b", re.I)
_OPTIONAL_RE = re.compile(
    r"\b(walk\s*-?\s*through|mod|patch(?:es)?|hotfix|cheat|gallery|"
    r"translation|uncensor|save|extra)\b",
    re.I,
)
_MERGE_ADDON_RE = re.compile(
    r"\b(?:mod|patch|hotfix|fix|cheat|gallery|unlock(?:er)?|translation|uncensor)\b",
    re.I,
)
_INCREMENTAL_UPDATE_RE = re.compile(
    r"\b(?:update|upgrade)\s+patch\b|\b(?:incremental|delta)\s+(?:patch|update|build)\b",
    re.I,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestAmbiguity(_StrictModel):
    description: BoundedText
    link_ids: list[ShortText] = Field(max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)


class ManifestVariant(_StrictModel):
    platform: ShortText | None
    # An empty list is how an artifact the post names but never links says so.
    # Only manual delivery may use it; validate_manifest_references enforces
    # that. Requiring a link id here is what pushed the model into pointing an
    # unlinked add-on at some other artifact's file.
    link_ids: list[ShortText] = Field(max_length=64)
    # Use the literal string "unresolved" when the links cannot safely be
    # identified as mirrors of one file.
    mirror_group: ShortText
    confidence: float = Field(ge=0.0, le=1.0)
    notes: list[BoundedText] = Field(max_length=8)


class _ManifestArtifactBase(_StrictModel):
    artifact_id: ShortText
    title: ShortText
    part_number: int | None
    part_label: ShortText | None
    version: ShortText | None
    variants: list[ManifestVariant] = Field(min_length=1, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguities: list[BoundedText] = Field(max_length=8)


class GameManifestArtifact(_ManifestArtifactBase):
    kind: Literal["game"]
    required: Literal[True]
    delivery: Literal["download"]
    install_action: Literal["game"]


class AddonManifestArtifact(_ManifestArtifactBase):
    kind: Literal["addon"]
    required: Literal[False]
    delivery: Literal["download", "manual"]
    install_action: Literal["merge", "separate", "manual"]


ManifestArtifact = Annotated[
    GameManifestArtifact | AddonManifestArtifact,
    Field(discriminator="kind"),
]


class DownloadManifest(_StrictModel):
    schema_version: Literal[1]
    thread_id: int
    title: ShortText
    multipart: bool
    artifacts: list[ManifestArtifact] = Field(max_length=96)
    ambiguities: list[ManifestAmbiguity] = Field(max_length=24)
    warnings: list[BoundedText] = Field(max_length=24)
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class RegisteredLink:
    link_id: str
    group_index: int
    group_name: str
    link_index: int
    label: str
    filename_hint: str | None
    locator: str

    def prompt_value(self) -> dict[str, object]:
        """Return only inert metadata; raw URLs never enter the model prompt."""
        return {
            "link_id": self.link_id,
            "label": self.label,
            "filename_hint": self.filename_hint,
        }


@dataclass(frozen=True)
class SourceGroup:
    index: int
    name: str
    links: tuple[RegisteredLink, ...]

    def prompt_value(self) -> dict[str, object]:
        return {
            "group_index": self.index,
            "name": self.name,
            "links": [link.prompt_value() for link in self.links],
        }


@dataclass(frozen=True)
class ForumSection:
    name: str
    groups: tuple[SourceGroup, ...]
    post_excerpt: str

    def prompt_value(self) -> dict[str, object]:
        return {
            "section": self.name,
            "post_excerpt": self.post_excerpt,
            "download_groups": [group.prompt_value() for group in self.groups],
        }


def build_link_registry(thread: ThreadInfo) -> tuple[RegisteredLink, ...]:
    records: list[RegisteredLink] = []
    for group_index, group in enumerate(thread.downloads):
        for link_index, mirror in enumerate(group.mirrors):
            records.append(
                RegisteredLink(
                    link_id=f"g{group_index:03d}l{link_index:03d}",
                    group_index=group_index,
                    group_name=group.name,
                    link_index=link_index,
                    label=mirror.name,
                    filename_hint=_filename_hint(mirror.locator),
                    locator=mirror.locator,
                )
            )
    return tuple(records)


def build_forum_sections(
    thread: ThreadInfo,
    post_text: str,
    *,
    max_groups: int,
    max_excerpt_chars: int,
) -> tuple[ForumSection, ...]:
    """Split structured groups on authored headings, then cap dense sections."""
    registry = build_link_registry(thread)
    links_by_group: dict[int, list[RegisteredLink]] = {}
    for link in registry:
        links_by_group.setdefault(link.group_index, []).append(link)
    source_groups = [
        SourceGroup(index, group.name, tuple(links_by_group.get(index, ())))
        for index, group in enumerate(thread.downloads)
    ]

    raw_sections: list[tuple[str, list[SourceGroup]]] = []
    current_name = "General downloads"
    current: list[SourceGroup] = []
    current_is_part = False

    def flush() -> None:
        nonlocal current
        if current:
            raw_sections.append((current_name, current))
            current = []

    for group in source_groups:
        heading_match = _PART_RE.search(group.name)
        if not group.links:
            flush()
            current_name = group.name.strip() or "Untitled section"
            current_is_part = bool(heading_match)
            current.append(group)
            continue
        if current_is_part and _OPTIONAL_RE.search(group.name):
            flush()
            current_name = "Optional downloads"
            current_is_part = False
        current.append(group)
        if len(current) >= max_groups:
            flush()
            current_name = f"{current_name} (continued)"

    flush()
    if not raw_sections and not source_groups:
        raw_sections.append(("General downloads", []))

    # A section holding no links cannot produce artifacts: the constrained
    # schema caps them at zero for exactly that case. Dropping it saves a model
    # call, and the preceding section's excerpt then runs on through the dropped
    # heading's text, so nothing the post said is lost. Keep the unfiltered list
    # when no section has links at all so a link-free thread still has a shape.
    linked_sections = [
        (name, groups)
        for name, groups in raw_sections
        if any(group.links for group in groups)
    ]
    if linked_sections:
        raw_sections = linked_sections

    names = [name for name, _groups in raw_sections]
    sections = []
    for index, (name, groups) in enumerate(raw_sections):
        next_name = names[index + 1] if index + 1 < len(names) else None
        sections.append(
            ForumSection(
                name=name,
                groups=tuple(groups),
                post_excerpt=_section_excerpt(
                    post_text,
                    name,
                    next_name,
                    fallback_names=tuple(group.name for group in groups),
                    max_chars=max_excerpt_chars,
                ),
            )
        )
    return tuple(sections)


def validate_manifest_references(
    manifest: DownloadManifest,
    *,
    thread_id: int,
    valid_link_ids: set[str],
    require_game: bool,
) -> None:
    if manifest.thread_id != thread_id:
        raise ValueError(
            f"Manifest named thread #{manifest.thread_id}, expected #{thread_id}"
        )
    artifact_ids: set[str] = set()
    referenced: set[str] = set()
    games = 0
    for artifact in manifest.artifacts:
        if artifact.artifact_id in artifact_ids:
            raise ValueError(f"Duplicate manifest artifact ID: {artifact.artifact_id}")
        artifact_ids.add(artifact.artifact_id)
        if artifact.kind == "game":
            games += 1
            if not artifact.required or artifact.install_action != "game":
                raise ValueError("Game artifacts must be required with action 'game'")
            if require_game and artifact.delivery != "download":
                raise ValueError("Final game artifacts must be downloadable")
        elif artifact.required:
            raise ValueError("Add-on artifacts cannot be required")
        for variant in artifact.variants:
            if not variant.link_ids and artifact.delivery == "download":
                raise ValueError(f"Download artifact {artifact.artifact_id} has no links")
            for link_id in variant.link_ids:
                if link_id not in valid_link_ids:
                    raise ValueError(f"Manifest returned unknown link ID: {link_id}")
                referenced.add(link_id)
    for ambiguity in manifest.ambiguities:
        unknown = set(ambiguity.link_ids) - valid_link_ids
        if unknown:
            raise ValueError(f"Manifest ambiguity returned unknown link ID: {min(unknown)}")
    if require_game and games == 0:
        raise ValueError("Final manifest did not contain a game artifact")
    if require_game and not referenced:
        raise ValueError("Final manifest did not reference any registered links")


def part_detection_from_manifest(manifest: DownloadManifest) -> PartDetection:
    parts: dict[int, str] = {}
    for artifact in manifest.artifacts:
        if artifact.kind != "game" or artifact.part_number is None:
            continue
        parts.setdefault(
            artifact.part_number,
            artifact.part_label or f"Part {artifact.part_number}",
        )
    if len(parts) < 2:
        return PartDetection(family=None, parts=())
    first_label = next(iter(parts.values()))
    family_match = re.match(r"([A-Za-z]+)", first_label)
    family = family_match.group(1).casefold() if family_match else "part"
    return PartDetection(
        family=family,
        parts=tuple(
            DetectedPart(number, label, ()) for number, label in sorted(parts.items())
        ),
    )


def build_download_plan_from_manifest(
    thread: ThreadInfo,
    manifest: DownloadManifest,
    *,
    platform_priority: list[str],
    preferred_hosts: list[str],
    selected_parts: tuple[int, ...] | None,
    include_addons: bool,
    discovered_addons: tuple[ThreadInfo, ...] = (),
) -> DownloadPlan:
    registry = {record.link_id: record for record in build_link_registry(thread)}
    discovered_by_id = {addon.thread_id: addon for addon in discovered_addons}
    consumed_addon_ids: set[int] = set()
    game_link_ids = {
        link_id
        for artifact in manifest.artifacts
        if artifact.kind == "game"
        for variant in artifact.variants
        for link_id in variant.link_ids
    }
    game_part_numbers = {
        artifact.part_number
        for artifact in manifest.artifacts
        if artifact.kind == "game" and artifact.part_number is not None
    }
    selected: list[PlannedArtifact] = []
    skipped: list[SkippedArtifact] = []
    for artifact in manifest.artifacts:
        if artifact.kind == "addon" and not include_addons:
            continue
        if artifact.kind == "addon" and _INCREMENTAL_UPDATE_RE.search(artifact.title):
            skipped.append(
                SkippedArtifact(
                    artifact.title,
                    "incremental update is not applicable when downloading a full build",
                )
            )
            continue
        if artifact.kind == "addon" and _duplicates_numbered_game(
            artifact.title,
            artifact.part_number,
            thread.title,
            game_part_numbers,
        ):
            continue
        if (
            selected_parts is not None
            and not _artifact_applies_to_parts(artifact, selected_parts)
        ):
            continue
        variant = _choose_variant(artifact, platform_priority)
        if variant is None:
            skipped.append(SkippedArtifact(artifact.title, "no compatible variant"))
            continue
        if (
            artifact.kind == "addon"
            and min(artifact.confidence, variant.confidence) < ADDON_CONFIDENCE_FLOOR
        ):
            skipped.append(
                SkippedArtifact(
                    artifact.title,
                    f"LLM classification confidence is below {ADDON_CONFIDENCE_FLOOR:.2f}",
                )
            )
            continue
        if artifact.kind == "addon" and game_link_ids.intersection(variant.link_ids):
            continue
        records = [registry[link_id] for link_id in variant.link_ids]
        records = _order_records(records, preferred_hosts)
        nested = (
            _plan_nested_addon(
                thread,
                artifact,
                records,
                discovered_by_id,
                preferred_hosts=preferred_hosts,
            )
            if artifact.kind == "addon"
            else None
        )
        if nested is not None:
            selected.append(nested)
            consumed_addon_ids.add(nested.thread_id)
            continue
        manual_document = (
            artifact.kind == "addon"
            and (artifact.delivery == "manual" or artifact.install_action == "manual")
            and _records_are_direct_documents(records)
        )
        effective_addon_action = (
            _effective_addon_action(artifact, records)
            if artifact.kind == "addon"
            else None
        )
        direct_addon = (
            artifact.kind == "addon"
            and bool(records)
            and all(
                is_likely_download_locator(record.locator, label=record.label)
                for record in records
            )
            and effective_addon_action is not None
        )
        if (
            artifact.delivery == "manual" or artifact.install_action == "manual"
        ) and not (manual_document or direct_addon):
            skipped.append(
                SkippedArtifact(artifact.title, _manual_reason(artifact))
            )
            continue
        unresolved_mirrors = (
            variant.mirror_group.casefold() == "unresolved"
            and len(variant.link_ids) > 1
        )
        recovered_source_mirrors = (
            unresolved_mirrors
            and _records_are_platform_mirrors(records, variant.platform)
        )
        if unresolved_mirrors and artifact.kind == "addon" and not recovered_source_mirrors:
            skipped.append(
                SkippedArtifact(
                    artifact.title,
                    "links could not safely be identified as mirrors",
                )
            )
            continue
        if unresolved_mirrors and not recovered_source_mirrors:
            # Selecting one required build does not assert that the remaining
            # links are mirrors. They stay unavailable as fallbacks until a
            # landing-page/filename pass can establish equivalence.
            records = records[:1]
        if any(_is_forum_page(record.locator) for record in records):
            skipped.append(
                SkippedArtifact(
                    artifact.title,
                    "link opens a forum page and requires manual handling",
                )
            )
            continue
        if not records:
            skipped.append(SkippedArtifact(artifact.title, "no downloadable links"))
            continue
        primary, *alternates = records
        warning_parts = (
            [] if artifact.kind == "game" else [*artifact.ambiguities, *variant.notes]
        )
        if recovered_source_mirrors:
            warning_parts.append(
                "mirror relationship recovered from one platform download group"
            )
        elif unresolved_mirrors:
            warning_parts.append(
                "mirror grouping is unresolved; using the preferred link without fallbacks"
            )
        if min(artifact.confidence, variant.confidence) < ADDON_CONFIDENCE_FLOOR:
            warning_parts.append(
                f"LLM classification confidence is below {ADDON_CONFIDENCE_FLOOR:.2f}"
            )
        install_action: Literal["merge", "separate"] | None = None
        if artifact.kind == "addon":
            install_action = effective_addon_action
        artifact_version = artifact.version
        if artifact.kind == "game" and artifact_version is None:
            artifact_version = _infer_part_version(
                artifact.part_number, registry.values()
            )
        selected_platform = _selected_platform(variant.platform, platform_priority)
        selected.append(
            PlannedArtifact(
                kind=artifact.kind,
                title=artifact.title,
                version=artifact_version,
                thread_id=thread.thread_id,
                thread_url=thread.url,
                group_name=primary.group_name or artifact.title,
                platform=selected_platform,
                host=primary.label,
                locator=primary.locator,
                warning="; ".join(warning_parts) or None,
                alternate_mirrors=tuple(
                    DownloadMirror(
                        record.label,
                        record.locator,
                        platform=selected_platform,
                        group_name=record.group_name,
                    )
                    for record in alternates
                ),
                part=(
                    artifact.part_label
                    or (
                        f"Part {artifact.part_number}"
                        if artifact.part_number is not None
                        else None
                    )
                ),
                install_action=install_action,
            )
        )
    if include_addons:
        selected.extend(
            _remaining_discovered_addons(
                thread,
                discovered_addons,
                consumed_addon_ids=consumed_addon_ids,
                selected=selected,
                preferred_hosts=preferred_hosts,
                skipped=skipped,
            )
        )
    selected, not_applicable_titles = _scope_versioned_addons(selected)
    if not any(artifact.kind == "game" for artifact in selected):
        from vnmaster.downloads.selector import NoCompatibleDownloadError

        raise NoCompatibleDownloadError(
            f"The interpreted manifest had no downloadable selected build for {thread.title!r}"
        )
    selected_titles = {artifact.title.casefold() for artifact in selected}
    visible_skips = [
        item
        for item in skipped
        if item.title.casefold() not in selected_titles
        and item.title.casefold() not in not_applicable_titles
    ]
    return DownloadPlan(thread, tuple(selected), _deduplicate_skipped(visible_skips))


def _plan_nested_addon(
    game: ThreadInfo,
    artifact: AddonManifestArtifact,
    records: list[RegisteredLink],
    discovered_by_id: dict[int, ThreadInfo],
    *,
    preferred_hosts: list[str],
) -> PlannedArtifact | None:
    """Replace a forum landing-page artifact with its child thread payload."""
    for record in records:
        if not _is_forum_page(record.locator):
            continue
        thread_id = extract_thread_id(record.locator)
        if thread_id is None or thread_id not in discovered_by_id:
            continue
        addon = discovered_by_id[thread_id]
        planned = select_addon_artifact(
            addon,
            preferred_hosts,
            warning=None,
        )
        if planned is None:
            continue
        compatible, compatibility_note = addon_matches_game(
            game, replace(addon, version=planned.version)
        )
        if not compatible:
            continue
        warning = compatibility_note or None
        notes = [
            *(part for part in (planned.warning, warning) if part),
            *artifact.ambiguities,
            *(note for variant in artifact.variants for note in variant.notes),
            "resolved from linked F95 add-on thread",
        ]
        return replace(
            planned,
            title=artifact.title,
            part=(
                artifact.part_label
                or (
                    f"Part {artifact.part_number}"
                    if artifact.part_number is not None
                    else None
                )
            ),
            warning="; ".join(dict.fromkeys(notes)) or None,
            install_action=_effective_addon_action(artifact, []),
        )
    return None


def _remaining_discovered_addons(
    game: ThreadInfo,
    discovered_addons: tuple[ThreadInfo, ...],
    *,
    consumed_addon_ids: set[int],
    selected: list[PlannedArtifact],
    preferred_hosts: list[str],
    skipped: list[SkippedArtifact],
) -> list[PlannedArtifact]:
    artifacts: list[PlannedArtifact] = []
    selected_locators = {artifact.locator for artifact in selected}
    for addon in discovered_addons:
        if addon.thread_id in consumed_addon_ids:
            continue
        planned = select_addon_artifact(
            addon,
            preferred_hosts,
            warning=None,
        )
        if planned is None:
            skipped.append(
                SkippedArtifact(addon.title, "no downloadable mirrors found")
            )
            continue
        compatible, reason = addon_matches_game(
            game, replace(addon, version=planned.version)
        )
        if not compatible:
            skipped.append(SkippedArtifact(addon.title, reason))
            continue
        if reason:
            planned = replace(planned, warning=reason)
        if planned.locator in selected_locators:
            continue
        selected_locators.add(planned.locator)
        artifacts.append(planned)
    return artifacts


def _effective_addon_action(
    artifact: AddonManifestArtifact,
    records: list[RegisteredLink],
) -> Literal["merge", "separate"] | None:
    if _records_are_direct_documents(records):
        return "separate"
    if artifact.install_action == "merge":
        return "merge"
    if artifact.install_action == "separate":
        return "separate"
    if _MERGE_ADDON_RE.search(artifact.title):
        return "merge"
    return None


def _scope_versioned_addons(
    selected: list[PlannedArtifact],
) -> tuple[list[PlannedArtifact], set[str]]:
    """Bind modifying add-ons to the selected part whose version they target."""
    games = [artifact for artifact in selected if artifact.kind == "game"]
    scoped: list[PlannedArtifact] = []
    not_applicable: set[str] = set()
    for artifact in selected:
        inferred_merge = artifact.install_action == "merge" or (
            artifact.install_action is None and _MERGE_ADDON_RE.search(artifact.title)
        )
        if artifact.kind != "addon" or artifact.part is not None or not inferred_merge:
            scoped.append(artifact)
            continue
        addon_versions = version_tokens(artifact.version)
        game_versions = [
            (game, version_tokens(game.version)) for game in games if game.part is not None
        ]
        if not addon_versions or not any(tokens for _game, tokens in game_versions):
            scoped.append(artifact)
            continue
        matches = [
            game
            for game, tokens in game_versions
            if tokens and addon_versions.intersection(tokens)
        ]
        if not matches:
            not_applicable.add(artifact.title.casefold())
            continue
        if len(matches) == 1:
            scoped.append(replace(artifact, part=matches[0].part))
            continue
        scoped.append(artifact)
    return scoped, not_applicable


def _artifact_applies_to_parts(
    artifact: ManifestArtifact,
    selected_parts: tuple[int, ...],
) -> bool:
    if artifact.part_number is not None:
        return artifact.part_number in selected_parts
    label = artifact.part_label or ""
    match = re.search(r"\bpart\s*(\d+)\s*(?:-\s*(\d+)|(\+))?", label, re.I)
    if match is None:
        return True
    start = int(match.group(1))
    if match.group(3):
        # A label such as "Part 2+" is not a mathematically defined range.
        # Keep it scoped to the named part unless stronger structured evidence
        # identifies exact additional targets.
        return start in selected_parts
    end = int(match.group(2) or start)
    return any(start <= number <= end for number in selected_parts)


def _records_are_platform_mirrors(
    records: list[RegisteredLink], platform: str | None
) -> bool:
    if not records or platform is None:
        return False
    if len({record.group_index for record in records}) != 1:
        return False
    group_tokens = set(re.findall(r"[a-z]+", records[0].group_name.casefold()))
    platform_tokens = set(re.findall(r"[a-z]+", platform.casefold()))
    aliases = {
        "mac": "mac",
        "macos": "mac",
        "osx": "mac",
        "win": "windows",
        "windows": "windows",
        "pc": "windows",
        "linux": "linux",
        "android": "android",
    }
    normalized_group = {aliases.get(token, token) for token in group_tokens}
    normalized_platform = {aliases.get(token, token) for token in platform_tokens}
    return bool(normalized_group & normalized_platform)


def _choose_variant(
    artifact: ManifestArtifact, platform_priority: list[str]
) -> ManifestVariant | None:
    if not artifact.variants:
        return None
    for wanted in platform_priority:
        matching = [
            variant
            for variant in artifact.variants
            if _platform_matches(variant.platform, wanted)
        ]
        if matching:
            return _most_confident(matching)
    neutral = [variant for variant in artifact.variants if variant.platform is None]
    if neutral:
        return _most_confident(neutral)
    if artifact.kind == "addon":
        return _most_confident(list(artifact.variants))
    return None


def _most_confident(variants: list[ManifestVariant]) -> ManifestVariant:
    """Pick the best-evidenced variant so one weak reading cannot sink an add-on.

    A low-confidence variant is dropped by the ADDON_CONFIDENCE_FLOOR gate in
    the caller. Taking the first listed variant let an unplaceable extra file
    discard the whole add-on even when a confident variant named the real
    payload. Ties keep the manifest's own order.
    """
    return max(variants, key=lambda variant: variant.confidence)


def _platform_matches(actual: str | None, wanted: str) -> bool:
    if actual is None:
        return False
    aliases = {
        "mac": ("mac", "macos", "osx"),
        "windows": ("win", "windows", "pc"),
        "linux": ("linux",),
        "android": ("android",),
    }
    normalized = actual.casefold()
    return any(alias in normalized for alias in aliases.get(wanted.casefold(), (wanted,)))


def _selected_platform(actual: str | None, platform_priority: list[str]) -> str | None:
    """Return the canonical platform name used by execution and verification."""
    if actual is None:
        return None
    aliases = {
        "mac": "mac",
        "macos": "mac",
        "osx": "mac",
        "win": "windows",
        "windows": "windows",
        "pc": "windows",
        "linux": "linux",
        "android": "android",
    }
    for wanted in platform_priority:
        if _platform_matches(actual, wanted):
            return aliases.get(wanted.casefold(), wanted.casefold())
    tokens = re.findall(r"[a-z]+", actual.casefold())
    return next((aliases[token] for token in tokens if token in aliases), actual.casefold())


def _order_records(
    records: list[RegisteredLink], preferred_hosts: list[str]
) -> list[RegisteredLink]:
    preferred = [
        record
        for host in preferred_hosts
        for record in records
        if host.casefold() in record.label.casefold()
    ]
    return [*dict.fromkeys(preferred), *(record for record in records if record not in preferred)]


def _manual_reason(artifact: ManifestArtifact) -> str:
    details = "; ".join(
        text[:200]
        for text in (*artifact.ambiguities, *(n for v in artifact.variants for n in v.notes))
    )[:500]
    if not any(variant.link_ids for variant in artifact.variants):
        headline = "named in the post, but the thread publishes no download link for it"
    else:
        headline = "manual download or installation required"
    return f"{headline}{': ' + details if details else ''}"


def _records_are_direct_documents(records: list[RegisteredLink]) -> bool:
    if not records:
        return False
    document_suffixes = {".pdf", ".txt", ".rtf", ".doc", ".docx"}
    for record in records:
        if record.locator.startswith("//a["):
            return False
        try:
            parsed = urlsplit(record.locator)
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold()
        if host != "attachments.f95zone.to" and not (
            host == "f95zone.to" and "/attachments/" in parsed.path.casefold()
        ):
            return False
        if not any(parsed.path.casefold().endswith(suffix) for suffix in document_suffixes):
            return False
    return True


def _infer_part_version(
    part_number: int | None, records: Iterable[RegisteredLink]
) -> str | None:
    if part_number is None:
        return None
    for record in records:
        filename = record.filename_hint
        if not filename:
            continue
        match = re.search(
            rf"p(?:art)?[ _-]*{part_number}[ _-]+(\d+(?:\.\d+)+)",
            filename,
            re.I,
        )
        if match:
            return f"v{match.group(1)}"
    return None


def _duplicates_numbered_game(
    title: str,
    part_number: int | None,
    thread_title: str,
    game_part_numbers: set[int],
) -> bool:
    if part_number is None or part_number not in game_part_numbers:
        return False
    normalized = re.sub(r"[^a-z0-9]+", "", title.casefold())
    game = re.sub(r"[^a-z0-9]+", "", thread_title.casefold())
    return normalized in {f"part{part_number}", f"{game}part{part_number}"}


def _deduplicate_skipped(skipped: list[SkippedArtifact]) -> tuple[SkippedArtifact, ...]:
    by_title: dict[str, SkippedArtifact] = {}
    order: list[str] = []
    for item in skipped:
        key = item.title.casefold()
        existing = by_title.get(key)
        if existing is None:
            by_title[key] = item
            order.append(key)
        elif _skip_reason_priority(item.reason) > _skip_reason_priority(existing.reason):
            by_title[key] = item
    return tuple(by_title[key] for key in order)


def _skip_reason_priority(reason: str) -> int:
    folded = reason.casefold()
    if "forum page" in folded:
        return 4
    if "manual" in folded:
        return 3
    if "safely be identified" in folded:
        return 2
    return 1


def _filename_hint(locator: str) -> str | None:
    if locator.startswith("//"):
        return None
    filename = unquote(urlsplit(locator).path.rsplit("/", 1)[-1]).strip()
    return filename or None


def _is_forum_page(locator: str) -> bool:
    if locator.startswith("//a["):
        return False
    try:
        parsed = urlsplit(locator)
    except ValueError:
        return False
    return (parsed.hostname or "").casefold() == "f95zone.to" and bool(
        re.search(r"/(?:threads|posts|post-)/?", parsed.path, re.I)
    )


def _section_excerpt(
    post_text: str,
    section_name: str,
    next_name: str | None,
    *,
    fallback_names: tuple[str, ...],
    max_chars: int,
) -> str:
    if not post_text:
        return ""
    folded = post_text.casefold()
    start = folded.find(section_name.casefold())
    if start < 0:
        for fallback_name in fallback_names:
            if not fallback_name:
                continue
            start = folded.find(fallback_name.casefold())
            if start >= 0:
                break
    if start < 0:
        return ""
    end = len(post_text)
    if next_name:
        candidate = folded.find(next_name.casefold(), start + len(section_name))
        if candidate >= 0:
            end = candidate
    return post_text[start:end][:max_chars]
