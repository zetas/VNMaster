"""Dispatch downloads to the adapter matching the resolved public URL."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from vnmaster.downloads.datanodes import download_datanodes, is_datanodes_url
from vnmaster.downloads.gallery import (
    GalleryCandidate,
    GalleryDownloadError,
    download_gallery,
    inspect_gallery,
    is_gallery_url,
)
from vnmaster.downloads.google_drive import download_google_drive, is_google_drive_url
from vnmaster.downloads.http import download_direct_https, is_safe_https_url
from vnmaster.downloads.mega import download_mega, is_mega_url
from vnmaster.downloads.models import PlannedArtifact
from vnmaster.downloads.pixeldrain import download_pixeldrain, is_pixeldrain_url
from vnmaster.downloads.vikingfile import download_vikingfile, is_vikingfile_url


class UnsupportedDownloadHostError(RuntimeError):
    pass


PayloadSelector = Callable[
    [PlannedArtifact, tuple[GalleryCandidate, ...]], tuple[GalleryCandidate, ...]
]
ArtifactDownloader = Callable[[PlannedArtifact, str, Path], list[Path]]


def is_url_for_host(host: str, url: str) -> bool:
    parsed = urlsplit(url)
    if (parsed.hostname or "").casefold() == "f95zone.to" and (
        "/threads/" in parsed.path.casefold()
        or "/posts/" in parsed.path.casefold()
        or "/post-" in parsed.path.casefold()
    ):
        # Forum pages are discovery/manual-install targets, never file payloads.
        return False
    normalized = host.casefold()
    if "mega" in normalized:
        return is_mega_url(url)
    if "pixeldrain" in normalized:
        return is_pixeldrain_url(url)
    if "google" in normalized or "drive" in normalized:
        return is_google_drive_url(url)
    if "gofile" in normalized or "mixdrop" in normalized:
        return is_gallery_url(url)
    if "datanodes" in normalized:
        return is_datanodes_url(url)
    if "viking" in normalized:
        return is_vikingfile_url(url) or is_safe_https_url(url)
    if "f95zone.to/masked/" in url.casefold():
        return False
    return is_safe_https_url(url)


def download_url(url: str, destination: Path) -> list[Path]:
    if is_mega_url(url):
        return download_mega(url, destination)
    if is_pixeldrain_url(url):
        return download_pixeldrain(url, destination)
    if is_google_drive_url(url):
        return download_google_drive(url, destination)
    if is_gallery_url(url):
        return download_gallery(url, destination)
    if is_datanodes_url(url):
        return download_datanodes(url, destination)
    if is_vikingfile_url(url):
        return download_vikingfile(url, destination)
    if is_safe_https_url(url):
        return download_direct_https(url, destination)
    raise UnsupportedDownloadHostError("Unsupported or unsafe download URL")


def download_artifact_url(
    artifact: PlannedArtifact,
    url: str,
    destination: Path,
    *,
    payload_selector: PayloadSelector | None = None,
) -> list[Path]:
    """Download one planned artifact, adjudicating mixed provider containers."""
    if not is_gallery_url(url):
        return download_url(url, destination)

    candidates = inspect_gallery(url)
    selected: tuple[GalleryCandidate, ...]
    if len(candidates) == 1:
        selected = candidates
    elif payload_selector is None:
        raise GalleryDownloadError(
            f"Provider container has {len(candidates)} files; manual payload "
            "selection is required"
        )
    else:
        selected = payload_selector(artifact, candidates)
    if not selected:
        raise GalleryDownloadError("No provider payload was selected")
    known = {candidate.candidate_id: candidate for candidate in candidates}
    for candidate in selected:
        if known.get(candidate.candidate_id) != candidate:
            raise GalleryDownloadError(
                "Payload selector returned a file outside the provider inventory"
            )
    return download_gallery(url, destination, selection=selected)
