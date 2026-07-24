from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from vnmaster.downloads.http import (
    DirectDownloadError,
    download_direct_https,
    is_safe_https_url,
)

PUBLIC_ADDRESS = [
    (2, 1, 6, "", ("93.184.216.34", 443)),
]


def test_safe_https_url_rejects_local_and_credentialed_urls() -> None:
    assert is_safe_https_url("https://files.example/game.zip")
    assert not is_safe_https_url("https://localhost/game.zip")
    assert not is_safe_https_url("https://127.0.0.1/game.zip")
    assert not is_safe_https_url("https://user:pass@example.com/game.zip")


def test_direct_download_uses_content_disposition_filename(tmp_path: Path) -> None:
    request_details: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_details.append(
            (
                request.url.host,
                request.headers["host"],
                request.extensions.get("sni_hostname"),
            )
        )
        return response

    response = httpx.Response(
        200,
        headers={
            "content-type": "application/zip",
            "content-disposition": 'attachment; filename="game.zip"',
        },
        content=b"archive",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        files = download_direct_https(
            "https://files.example/download",
            tmp_path,
            client=client,
            resolver=lambda *_args, **_kwargs: PUBLIC_ADDRESS,
        )
    assert files == [tmp_path / "game.zip"]
    assert files[0].read_bytes() == b"archive"
    assert request_details == [
        ("93.184.216.34", "files.example", "files.example")
    ]


def test_direct_download_rejects_host_landing_page(tmp_path: Path) -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/html"},
        text="<html>not a file</html>",
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(DirectDownloadError, match="dedicated adapter"):
            download_direct_https(
                "https://files.example/download",
                tmp_path,
                client=client,
                resolver=lambda *_args, **_kwargs: PUBLIC_ADDRESS,
            )


def test_direct_download_rejects_hostname_resolving_to_private_address(
    tmp_path: Path,
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"not reached")

    private_address = [(2, 1, 6, "", ("127.0.0.1", 443))]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DirectDownloadError, match="non-public"):
            download_direct_https(
                "https://files.example/game.zip",
                tmp_path,
                client=client,
                resolver=lambda *_args, **_kwargs: private_address,
            )
    assert called is False


def test_direct_download_validates_every_redirect_destination(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.headers["host"])
        return httpx.Response(
            302,
            headers={"location": "https://internal.example/game.zip"},
        )

    def resolver(host: str, *_args, **_kwargs):
        address = "127.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(2, 1, 6, "", (address, 443))]

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DirectDownloadError, match="non-public"):
            download_direct_https(
                "https://files.example/download",
                tmp_path,
                client=client,
                resolver=resolver,
            )
    assert requested == ["files.example"]
