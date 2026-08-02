from pathlib import Path

import pytest

import vnmaster.downloads.downloader as downloader_module
from vnmaster.downloads.downloader import download_artifact_url, is_url_for_host
from vnmaster.downloads.gallery import GalleryCandidate, GalleryDownloadError
from vnmaster.downloads.models import PlannedArtifact


def _artifact() -> PlannedArtifact:
    return PlannedArtifact(
        kind="game",
        title="A Game",
        version="v1.2",
        thread_id=42,
        thread_url="https://f95zone.to/threads/.42/",
        group_name="Mac",
        platform="mac",
        host="GOFILE",
        locator="https://gofile.io/d/abc",
        part="Part 7",
    )


def test_forum_thread_is_never_treated_as_a_download_file() -> None:
    assert not is_url_for_host(
        "WALKTHROUGH MOD",
        "https://f95zone.to/threads/grandmas-house-walkthrough-mod.107512/",
    )


def test_f95_attachment_remains_a_supported_https_file() -> None:
    assert is_url_for_host(
        "F95 ATTACHMENT",
        "https://attachments.f95zone.to/2026/01/guide.pdf",
    )


def test_mixed_gallery_container_requires_payload_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = (
        GalleryCandidate("payload-0001", 1, "A Game-1.2-mac.zip", 100),
        GalleryCandidate("payload-0002", 2, "A Game-1.2-pc.zip", 100),
    )
    monkeypatch.setattr(downloader_module, "inspect_gallery", lambda _url: candidates)

    with pytest.raises(GalleryDownloadError, match="manual payload selection"):
        download_artifact_url(
            _artifact(), "https://gofile.io/d/abc", tmp_path / "download"
        )


def test_mixed_gallery_container_downloads_only_adjudicated_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = (
        GalleryCandidate("payload-0001", 1, "A Game-1.2-mac.zip", 100),
        GalleryCandidate("payload-0002", 2, "A Game-1.2-pc.zip", 100),
    )
    selected: list[tuple[GalleryCandidate, ...]] = []

    monkeypatch.setattr(downloader_module, "inspect_gallery", lambda _url: candidates)

    def fake_download(
        _url: str,
        destination: Path,
        *,
        selection: tuple[GalleryCandidate, ...] | None = None,
    ) -> list[Path]:
        assert selection is not None
        selected.append(selection)
        destination.mkdir(parents=True)
        payload = destination / selection[0].filename
        payload.write_bytes(b"archive")
        return [payload]

    monkeypatch.setattr(downloader_module, "download_gallery", fake_download)
    downloaded = download_artifact_url(
        _artifact(),
        "https://gofile.io/d/abc",
        tmp_path / "download",
        payload_selector=lambda _artifact, items: (items[0],),
    )

    assert selected == [(candidates[0],)]
    assert [path.name for path in downloaded] == ["A Game-1.2-mac.zip"]
