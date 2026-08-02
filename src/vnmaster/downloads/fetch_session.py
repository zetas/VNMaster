"""UI-independent orchestration for interactive fetch frontends."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
import re
import time
from typing import Literal, Protocol

import httpx
from sqlalchemy import Engine

from vnmaster.config import Config, Secrets
from vnmaster.db.engine import create_engine_for, ensure_schema
from vnmaster.downloads.downloader import is_url_for_host
from vnmaster.downloads.f95 import fetch_starter_post_text, resolve_redacted_locator
from vnmaster.downloads.manifest import (
    DownloadManifest,
    build_download_plan_from_manifest,
    part_detection_from_manifest,
)
from vnmaster.downloads.models import (
    DownloadMirror,
    DownloadPlan,
    PartDetection,
    ResolvedDownload,
)
from vnmaster.downloads.selector import detect_parts
from vnmaster.downloads.service import (
    DownloadExecutionResult,
    execute_download_plan_detailed,
    execute_multipart_plan,
    execute_optional_downloads,
)
from vnmaster.downloads.state import list_install_states, save_install_state
from vnmaster.downloads.workflow import (
    ThreadDiscovery,
    build_plan_from_discovery,
    discover_thread,
)
from vnmaster.f95_search import build_search_client
from vnmaster.llm.forum_manifest import ForumManifestInterpreter
from vnmaster.llm.structured import StructuredOutputClient
from vnmaster.paths import VNMasterPaths


Reporter = Callable[[str], None]


@dataclass(frozen=True)
class FetchSnapshot:
    """Discovery and interpretation results used to build one or more plans."""

    discovery: ThreadDiscovery
    manifest: DownloadManifest | None
    detection: PartDetection
    parser_summary: str | None
    notes: tuple[str, ...]
    installed_parts: Mapping[int, str]
    include_addons: bool = True


@dataclass(frozen=True)
class ProtectedDownload:
    """A masked F95 link that requires user completion in a browser."""

    artifact_index: int
    artifact_title: str
    mirror: DownloadMirror
    protected_url: str


@dataclass(frozen=True)
class ResolutionResult:
    downloads: tuple[tuple[ResolvedDownload, ...], ...]
    protected: tuple[ProtectedDownload, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.protected and not self.errors and all(self.downloads)


@dataclass(frozen=True)
class FetchRunResult:
    final_dirs: tuple[Path, ...]
    install_ids: tuple[int, ...]
    failures: tuple[str, ...] = ()
    mode: Literal["game", "optionals"] = "game"


class FetchBackend(Protocol):
    """Small synchronous API used by the Textual frontend and its test fake."""

    destination: Path
    excluded_hosts: tuple[str, ...]

    def discover(self, query: str, *, include_addons: bool = True) -> FetchSnapshot: ...

    def build_plan(
        self, snapshot: FetchSnapshot, selected_parts: tuple[int, ...] | None
    ) -> DownloadPlan: ...

    def resolve_plan(
        self,
        plan: DownloadPlan,
        supplied_urls: Mapping[int, str] | None = None,
    ) -> ResolutionResult: ...

    def execute(
        self,
        plan: DownloadPlan,
        resolved_downloads: tuple[tuple[ResolvedDownload, ...], ...],
    ) -> FetchRunResult: ...

    def close(self) -> None: ...


class VNMasterFetchBackend:
    """Production fetch orchestration with no Click or Textual dependencies."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        destination: Path | None = None,
        reporter: Reporter = lambda _message: None,
    ) -> None:
        paths = VNMasterPaths.defaults_for_macos()
        self.config = Config.load(config_path or paths.config_dir / "config.toml")
        self.secrets = Secrets.load(paths.config_dir / "secrets.toml")
        self.engine = create_engine_for(self.config.paths.vnmaster_db)
        ensure_schema(self.engine)
        self.destination = (destination or self.config.downloads.destination).expanduser()
        self.excluded_hosts = tuple(self.config.downloads.excluded_hosts)
        self._reporter = reporter

    def discover(self, query: str, *, include_addons: bool = True) -> FetchSnapshot:
        with build_search_client(cookie_header=self.secrets.f95zone_cookies) as client:
            discovery = discover_thread(query, client=client, include_addons=include_addons)
            manifest: DownloadManifest | None = None
            parser_summary: str | None = None
            parser_settings = self.config.downloads.forum_parser
            if parser_settings.enabled:
                try:
                    post_text = fetch_starter_post_text(discovery.game.url, client=client)
                    with StructuredOutputClient(parser_settings, self.secrets) as generator:
                        interpreter = ForumManifestInterpreter(
                            generator,
                            parser_settings,
                            self.engine,
                            clock=lambda: int(time.time()),
                            reporter=self._reporter,
                        )
                        interpretation = interpreter.interpret(discovery.game, post_text)
                    manifest = interpretation.manifest
                    source = (
                        "cache"
                        if interpretation.cached
                        else f"{interpretation.calls} constrained call"
                        f"{'s' if interpretation.calls != 1 else ''}"
                    )
                    parser_summary = (
                        f"{parser_settings.provider}/{parser_settings.model} ({source})"
                    )
                except Exception as exc:
                    detail = " ".join(str(exc).split()) or type(exc).__name__
                    self._reporter(
                        "Schema-constrained forum parsing failed; using deterministic "
                        f"rules instead ({detail})."
                    )

        detection = (
            part_detection_from_manifest(manifest)
            if manifest is not None
            else detect_parts(discovery.game.downloads)
        )
        notes = list(detection.warnings)
        if any(group.name == "Thread download links" for group in discovery.game.downloads):
            notes.append(
                "This thread's links were hand-scraped; multi-part detection is unavailable."
            )
        return FetchSnapshot(
            discovery=discovery,
            manifest=manifest,
            detection=detection,
            parser_summary=parser_summary,
            notes=tuple(notes),
            installed_parts=_installed_part_versions(self.engine, discovery.game.thread_id),
            include_addons=include_addons,
        )

    def build_plan(
        self, snapshot: FetchSnapshot, selected_parts: tuple[int, ...] | None
    ) -> DownloadPlan:
        if snapshot.detection.is_multipart and not selected_parts:
            raise ValueError("Select at least one game part.")
        if snapshot.manifest is not None:
            plan = build_download_plan_from_manifest(
                snapshot.discovery.game,
                snapshot.manifest,
                platform_priority=self.config.downloads.platform_priority,
                preferred_hosts=self.config.downloads.preferred_hosts,
                selected_parts=selected_parts,
                include_addons=snapshot.include_addons,
                discovered_addons=snapshot.discovery.addons,
            )
            if snapshot.discovery.skipped:
                plan = replace(
                    plan,
                    skipped=plan.skipped + snapshot.discovery.skipped,
                )
            return plan
        return build_plan_from_discovery(
            snapshot.discovery,
            platform_priority=self.config.downloads.platform_priority,
            preferred_hosts=self.config.downloads.preferred_hosts,
            detection=(snapshot.detection if snapshot.detection.is_multipart else None),
            selected_parts=selected_parts,
            include_addons=snapshot.include_addons,
        )

    def resolve_plan(
        self,
        plan: DownloadPlan,
        supplied_urls: Mapping[int, str] | None = None,
    ) -> ResolutionResult:
        supplied_urls = supplied_urls or {}
        downloads: list[tuple[ResolvedDownload, ...]] = []
        protected: list[ProtectedDownload] = []
        errors: list[str] = []
        with build_search_client(cookie_header=self.secrets.f95zone_cookies) as client:
            for index, artifact in enumerate(plan.artifacts):
                supplied = supplied_urls.get(index)
                if supplied is not None:
                    mirror = next(
                        (
                            candidate
                            for candidate in artifact.mirrors
                            if is_url_for_host(candidate.name, supplied)
                        ),
                        None,
                    )
                    if mirror is None:
                        downloads.append(())
                        errors.append(
                            f"The pasted URL is not valid for any enabled provider "
                            f"for {artifact.title!r}."
                        )
                    else:
                        downloads.append((_resolved(mirror, supplied),))
                    continue

                candidates: list[ResolvedDownload] = []
                protected_for_artifact: list[ProtectedDownload] = []
                for mirror in artifact.mirrors:
                    try:
                        locator = resolve_redacted_locator(
                            mirror.locator,
                            thread_url=artifact.thread_url,
                            client=client,
                        )
                    except (RuntimeError, httpx.HTTPError) as exc:
                        detail = " ".join(str(exc).split()) or type(exc).__name__
                        self._reporter(
                            f"Could not resolve {mirror.name} for {artifact.title!r}: {detail}"
                        )
                        continue
                    if is_url_for_host(mirror.name, locator):
                        candidates.append(_resolved(mirror, locator))
                    elif "/masked/" in locator:
                        protected_for_artifact.append(
                            ProtectedDownload(index, artifact.title, mirror, locator)
                        )
                    else:
                        self._reporter(
                            f"Skipping {mirror.name} for {artifact.title!r}: "
                            "the link did not resolve to a supported URL."
                        )
                downloads.append(tuple(candidates))
                if not candidates:
                    if protected_for_artifact:
                        protected.append(protected_for_artifact[0])
                    else:
                        errors.append(f"No mirrors for {artifact.title!r} could be resolved.")
        return ResolutionResult(
            downloads=tuple(downloads),
            protected=tuple(protected),
            errors=tuple(errors),
        )

    def execute(
        self,
        plan: DownloadPlan,
        resolved_downloads: tuple[tuple[ResolvedDownload, ...], ...],
    ) -> FetchRunResult:
        if len(resolved_downloads) != len(plan.artifacts) or any(
            not candidates for candidates in resolved_downloads
        ):
            raise ValueError("Every artifact must have a resolved download.")
        candidate_list = list(resolved_downloads)
        if plan.artifacts and all(artifact.kind == "addon" for artifact in plan.artifacts):
            optional_result = execute_optional_downloads(
                plan,
                resolved_downloads=candidate_list,
                destination_root=self.destination,
                reporter=self._reporter,
            )
            return FetchRunResult(
                final_dirs=optional_result.completed,
                install_ids=(),
                failures=tuple(
                    f"Optional {failure.part}: {failure.error}"
                    for failure in optional_result.failures
                ),
                mode="optionals",
            )
        multipart = any(artifact.kind == "game" and artifact.part for artifact in plan.artifacts)
        if multipart:
            saved_ids: list[int] = []

            def record(part: str, result: DownloadExecutionResult) -> None:
                state = save_install_state(
                    self.engine,
                    result,
                    part=part,
                    install_root=result.final_dir.parent,
                    reporter=self._reporter,
                )
                saved_ids.append(state.id)

            result = execute_multipart_plan(
                plan,
                resolved_downloads=candidate_list,
                destination_root=self.destination,
                urm_mods_dir=self.config.paths.games_root / "Mods",
                reporter=self._reporter,
                on_part_complete=record,
            )
            return FetchRunResult(
                final_dirs=tuple(item.final_dir for item in result.completed),
                install_ids=tuple(saved_ids),
                failures=tuple(
                    (
                        f"Optional {failure.part}: {failure.error}"
                        if failure.kind == "addon"
                        else f"{failure.part}: {failure.error}"
                    )
                    for failure in result.failures
                ),
            )

        execution = execute_download_plan_detailed(
            plan,
            resolved_downloads=candidate_list,
            destination_root=self.destination,
            urm_mods_dir=self.config.paths.games_root / "Mods",
            reporter=self._reporter,
        )
        state = save_install_state(self.engine, execution, reporter=self._reporter)
        return FetchRunResult(
            (execution.final_dir,),
            (state.id,),
            failures=tuple(
                f"Optional {failure.part}: {failure.error}" for failure in execution.failures
            ),
        )

    def close(self) -> None:
        self.engine.dispose()


def _resolved(mirror: DownloadMirror, url: str) -> ResolvedDownload:
    return ResolvedDownload(
        mirror.name,
        mirror.locator,
        url,
        platform=mirror.platform,
        group_name=mirror.group_name,
    )


def _installed_part_versions(engine: Engine, thread_id: int) -> dict[int, str]:
    installed: dict[int, str] = {}
    for state in list_install_states(engine):
        if state.f95_thread_id != thread_id or not state.version:
            continue
        for entry in state.artifacts:
            label = entry.get("part")
            if not isinstance(label, str):
                continue
            match = re.search(r"(\d+)\s*$", label)
            if match:
                installed[int(match.group(1))] = state.version
    return installed
