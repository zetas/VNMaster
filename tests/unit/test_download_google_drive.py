from __future__ import annotations

from pathlib import Path

from vnmaster.downloads.google_drive import (
    download_google_drive,
    is_google_drive_url,
)


def test_is_google_drive_url_rejects_lookalike_hosts() -> None:
    assert is_google_drive_url("https://drive.google.com/file/d/abc/view")
    assert not is_google_drive_url("https://drive.google.com.evil.test/file/d/abc")


def test_download_google_drive_uses_inferred_output_name(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    calls: list[dict[str, object]] = []

    def downloader(**kwargs) -> str:
        calls.append(kwargs)
        output = destination / "translation.zip"
        output.write_bytes(b"payload")
        return str(output)

    files = download_google_drive(
        "https://drive.google.com/file/d/abc/view",
        destination,
        downloader=downloader,
    )
    assert files == [destination / "translation.zip"]
    assert calls[0]["resume"] is True
