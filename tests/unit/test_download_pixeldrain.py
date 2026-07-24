from __future__ import annotations

from pathlib import Path

import httpx

from vnmaster.downloads.pixeldrain import (
    download_pixeldrain,
    is_pixeldrain_url,
    pixeldrain_file_id,
)

PUBLIC_ADDRESS = [(2, 1, 6, "", ("93.184.216.34", 443))]


def test_pixeldrain_file_id_accepts_viewer_and_api_urls() -> None:
    assert pixeldrain_file_id("https://pixeldrain.com/u/Ab_12-c") == "Ab_12-c"
    assert pixeldrain_file_id("https://pixeldrain.com/api/file/Ab_12-c") == "Ab_12-c"
    assert not is_pixeldrain_url("https://example.com/u/Ab_12-c")


def test_download_pixeldrain_uses_metadata_name_and_verifies_size(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={"success": True, "name": "game.zip", "size": 7},
            )
        assert request.url.path == "/api/file/Ab_12-c"
        return httpx.Response(200, content=b"archive")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        files = download_pixeldrain(
            "https://pixeldrain.com/u/Ab_12-c",
            tmp_path,
            client=client,
            resolver=lambda *_args, **_kwargs: PUBLIC_ADDRESS,
        )
    assert files == [tmp_path / "game.zip"]
    assert files[0].read_bytes() == b"archive"
