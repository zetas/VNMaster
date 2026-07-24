from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from vnmaster.downloads.vikingfile import (
    VikingFileDownloadError,
    download_vikingfile,
    is_vikingfile_url,
)

PUBLIC_ADDRESS = [(2, 1, 6, "", ("93.184.216.34", 443))]


def test_recognizes_vikingfile_urls() -> None:
    assert is_vikingfile_url("https://vikingfile.com/f/YLl7EboqiK")
    assert is_vikingfile_url("https://vik1ngfile.site/f/YLl7EboqiK")
    assert not is_vikingfile_url("https://vikingfile.com/premium")


def test_reports_live_file_that_requires_turnstile(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/check-file":
            return httpx.Response(
                200,
                json=[{"exist": True, "name": "game.zip", "size": 42}],
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<div id="captcha-download"><script src="turnstile"></script></div>',
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VikingFileDownloadError, match="Turnstile"):
            download_vikingfile(
                "https://vikingfile.com/f/YLl7EboqiK",
                tmp_path,
                client=client,
                resolver=lambda *_args, **_kwargs: PUBLIC_ADDRESS,
            )


def test_reports_expired_vikingfile_file(tmp_path: Path) -> None:
    response = httpx.Response(200, json=[{"exist": False}])
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(VikingFileDownloadError, match="has expired"):
            download_vikingfile(
                "https://vikingfile.com/f/missing",
                tmp_path,
                client=client,
                resolver=lambda *_args, **_kwargs: PUBLIC_ADDRESS,
            )
