from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class DownloadMirror:
    name: str
    locator: str
    platform: str | None = None
    group_name: str | None = None


@dataclass(frozen=True)
class DownloadGroup:
    name: str
    mirrors: tuple[DownloadMirror, ...]


@dataclass(frozen=True)
class DetectedPart:
    number: int
    label: str
    group_indexes: tuple[int, ...]


@dataclass(frozen=True)
class PartDetection:
    family: str | None
    parts: tuple[DetectedPart, ...]
    warnings: tuple[str, ...] = ()

    @property
    def is_multipart(self) -> bool:
        return len(self.parts) >= 2


@dataclass(frozen=True)
class ThreadInfo:
    thread_id: int
    title: str
    version: str | None
    thread_type: int | None
    url: str
    downloads: tuple[DownloadGroup, ...]


@dataclass(frozen=True)
class ForumThread:
    thread_id: int
    title: str
    url: str


@dataclass(frozen=True)
class PlannedArtifact:
    kind: Literal["game", "addon"]
    title: str
    version: str | None
    thread_id: int
    thread_url: str
    group_name: str
    platform: str | None
    host: str
    locator: str
    warning: str | None = None
    alternate_mirrors: tuple[DownloadMirror, ...] = field(default_factory=tuple)

    @property
    def mirrors(self) -> tuple[DownloadMirror, ...]:
        """Return the selected mirror followed by its ordered fallbacks."""
        primary = DownloadMirror(
            self.host,
            self.locator,
            platform=self.platform,
            group_name=self.group_name,
        )
        return (primary, *self.alternate_mirrors)


@dataclass(frozen=True)
class ResolvedDownload:
    host: str
    locator: str
    url: str
    platform: str | None = None
    group_name: str | None = None


@dataclass(frozen=True)
class SkippedArtifact:
    title: str
    reason: str


@dataclass(frozen=True)
class DownloadPlan:
    game: ThreadInfo
    artifacts: tuple[PlannedArtifact, ...]
    skipped: tuple[SkippedArtifact, ...] = field(default_factory=tuple)
