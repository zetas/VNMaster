"""Dispatch downloads to the adapter matching the resolved public URL."""
from __future__ import annotations

from pathlib import Path

from vnmaster.downloads.datanodes import download_datanodes, is_datanodes_url
from vnmaster.downloads.gallery import download_gallery, is_gallery_url
from vnmaster.downloads.google_drive import download_google_drive, is_google_drive_url
from vnmaster.downloads.http import download_direct_https, is_safe_https_url
from vnmaster.downloads.mega import download_mega, is_mega_url
from vnmaster.downloads.pixeldrain import download_pixeldrain, is_pixeldrain_url
from vnmaster.downloads.vikingfile import download_vikingfile, is_vikingfile_url


class UnsupportedDownloadHostError(RuntimeError):
    pass


def is_url_for_host(host: str, url: str) -> bool:
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
