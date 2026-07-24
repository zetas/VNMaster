"""Adapter for public file hosts supported by gallery-dl."""
from __future__ import annotations

# The gallery-dl module is invoked with a fixed argument array, never a shell.
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit


_SUPPORTED_HOSTS = {
    "gofile.io",
    "www.gofile.io",
    "mixdrop.ag",
    "www.mixdrop.ag",
    "mixdrop.bz",
    "www.mixdrop.bz",
    "mixdrop.com",
    "www.mixdrop.com",
    "mixdrop.net",
    "www.mixdrop.net",
    "mixdrop.top",
    "www.mixdrop.top",
    "m1xdrop.ag",
    "www.m1xdrop.ag",
}


class GalleryDownloadError(RuntimeError):
    pass


def is_gallery_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() in _SUPPORTED_HOSTS


def download_gallery(
    url: str,
    destination: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[Path]:
    if not is_gallery_url(url):
        raise GalleryDownloadError("Expected a supported public GoFile or MixDrop URL")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise GalleryDownloadError(
            f"Download staging directory is not empty: {destination}"
        )

    result = runner(
        [
            sys.executable,
            "-m",
            "gallery_dl",
            "--config-ignore",
            "--no-input",
            "--no-mtime",
            "--directory",
            str(destination),
            url.strip(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = _last_error_line(result.stderr or result.stdout or "")
        if "mixdrop" in url.casefold() and "NoneType" in detail:
            detail = (
                "MixDrop changed to a browser reCAPTCHA ticket flow that "
                "gallery-dl cannot currently resolve unattended"
            )
        message = f"gallery-dl failed with exit status {result.returncode}"
        raise GalleryDownloadError(f"{message}: {detail}" if detail else message)

    downloaded = sorted(
        path
        for path in destination.rglob("*")
        if path.is_file() and not path.name.endswith((".part", ".tmp"))
    )
    if not downloaded:
        raise GalleryDownloadError("gallery-dl reported success but downloaded no files")
    return downloaded


def _last_error_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:500] if lines else ""
