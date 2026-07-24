from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vnmaster.downloads.gallery import (
    GalleryDownloadError,
    download_gallery,
    is_gallery_url,
)


def test_recognizes_supported_urls() -> None:
    assert is_gallery_url("https://gofile.io/d/abc")
    assert is_gallery_url("https://mixdrop.ag/f/abc")
    assert not is_gallery_url("http://gofile.io/d/abc")
    assert not is_gallery_url("https://example.com/d/abc")


def test_download_gallery_returns_all_downloaded_files(tmp_path: Path) -> None:
    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        destination = Path(command[command.index("--directory") + 1])
        (destination / "nested").mkdir()
        (destination / "nested" / "game.zip").write_bytes(b"archive")
        return subprocess.CompletedProcess(command, 0, "", "")

    downloaded = download_gallery(
        "https://gofile.io/d/abc", tmp_path / "download", runner=runner
    )
    assert downloaded == [tmp_path / "download" / "nested" / "game.zip"]


def test_download_gallery_reports_provider_failure(tmp_path: Path) -> None:
    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 4, "", "[gofile][error] Requested content could not be found\n"
        )

    with pytest.raises(GalleryDownloadError, match="content could not be found"):
        download_gallery(
            "https://gofile.io/d/missing", tmp_path / "download", runner=runner
        )


def test_download_gallery_explains_current_mixdrop_ticket_failure(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "AttributeError: 'NoneType' object has no attribute 'split'\n"
        )

    with pytest.raises(GalleryDownloadError, match="reCAPTCHA ticket flow"):
        download_gallery(
            "https://mixdrop.ag/f/missing", tmp_path / "download", runner=runner
        )
