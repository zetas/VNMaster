"""Adapter for public Google Drive file links."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from gdown.download import download as gdown_download


class GoogleDriveDownloadError(RuntimeError):
    pass


def is_google_drive_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() == "drive.google.com"


def download_google_drive(
    url: str,
    destination: Path,
    *,
    downloader: Callable[..., object] = gdown_download,
) -> list[Path]:
    if not is_google_drive_url(url):
        raise GoogleDriveDownloadError("Expected an HTTPS Google Drive file link")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise GoogleDriveDownloadError(
            f"Download staging directory is not empty: {destination}"
        )
    result = downloader(
        url=url.strip(),
        output=f"{destination}/",
        quiet=False,
        resume=True,
    )
    if not isinstance(result, str):
        raise GoogleDriveDownloadError("Google Drive download failed")
    downloaded = Path(result)
    if not downloaded.is_file() or downloaded.parent != destination:
        raise GoogleDriveDownloadError("Google Drive returned an unexpected output path")
    return [downloaded]
