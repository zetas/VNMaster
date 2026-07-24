from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vnmaster.downloads.mega import MegaDownloadError, download_mega, is_mega_url


def test_is_mega_url_accepts_only_https_mega_hosts() -> None:
    assert is_mega_url("https://mega.nz/file/abc#key")
    assert is_mega_url("https://www.mega.co.nz/folder/abc#key")
    assert not is_mega_url("http://mega.nz/file/abc")
    assert not is_mega_url("https://mega.nz.evil.example/file/abc")


def test_download_mega_invokes_command_without_a_shell(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(args)
        (destination / "game.zip").write_bytes(b"payload")
        return subprocess.CompletedProcess(args, 0)

    files = download_mega(
        "https://mega.nz/file/abc#key",
        destination,
        executable=Path("/fake/mega-get"),
        runner=runner,
    )
    assert calls == [[
        "/fake/mega-get", "https://mega.nz/file/abc#key", str(destination)
    ]]
    assert files == [destination / "game.zip"]


def test_download_mega_rejects_non_mega_url(tmp_path: Path) -> None:
    with pytest.raises(MegaDownloadError, match="Expected"):
        download_mega("https://example.com/file", tmp_path / "download")


def test_download_mega_reports_command_failure(tmp_path: Path) -> None:
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 7)

    with pytest.raises(MegaDownloadError, match="status 7"):
        download_mega(
            "https://mega.nz/file/abc#key",
            tmp_path / "download",
            executable=Path("/fake/mega-get"),
            runner=runner,
        )
