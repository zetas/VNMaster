from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from vnmaster.downloads.datanodes import (
    DataNodesDownloadError,
    download_datanodes,
    is_datanodes_url,
)

PUBLIC_ADDRESS = [(2, 1, 6, "", ("93.184.216.34", 443))]


def test_recognizes_datanodes_file_urls() -> None:
    assert is_datanodes_url("https://datanodes.to/abc123def456/game.zip")
    assert not is_datanodes_url("https://datanodes.to/download")
    assert not is_datanodes_url("http://datanodes.to/abc123def456/game.zip")


def test_reports_expired_datanodes_file(tmp_path: Path) -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/html"},
        text='<a href="/premium?from=deadfile">Files here expire fast</a>',
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(DataNodesDownloadError, match="has expired"):
            download_datanodes(
                "https://datanodes.to/abc123def456/game.zip",
                tmp_path,
                client=client,
                resolver=lambda *_args, **_kwargs: PUBLIC_ADDRESS,
            )


def test_downloads_direct_datanodes_response(tmp_path: Path) -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "application/zip"},
        content=b"archive",
        request=httpx.Request("GET", "https://cdn.datanodes.to/game.zip"),
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        downloaded = download_datanodes(
            "https://datanodes.to/abc123def456/game.zip",
            tmp_path,
            client=client,
            resolver=lambda *_args, **_kwargs: PUBLIC_ADDRESS,
        )
    assert downloaded == [tmp_path / "game.zip"]
    assert downloaded[0].read_bytes() == b"archive"


def test_datanodes_blocks_redirect_to_private_network(tmp_path: Path) -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.headers["host"])
        return httpx.Response(
            302,
            headers={"location": "https://internal.example/game.zip"},
        )

    def resolver(host: str, *_args, **_kwargs):
        address = "127.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(2, 1, 6, "", (address, 443))]

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DataNodesDownloadError, match="non-public"):
            download_datanodes(
                "https://datanodes.to/abc123def456/game.zip",
                tmp_path,
                client=client,
                resolver=resolver,
            )
    assert requested_hosts == ["datanodes.to"]
