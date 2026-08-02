from __future__ import annotations

from types import SimpleNamespace

import httpx

from vnmaster.downloads.fetch_session import VNMasterFetchBackend
from vnmaster.downloads.models import DownloadPlan, PlannedArtifact, ThreadInfo


def _plan(locator: str) -> DownloadPlan:
    game = ThreadInfo(
        1,
        "A Game",
        "v1",
        None,
        "https://f95zone.to/threads/.1/",
        (),
    )
    artifact = PlannedArtifact(
        kind="game",
        title="A Game",
        version="v1",
        thread_id=1,
        thread_url=game.url,
        group_name="Mac",
        platform="mac",
        host="MEGA",
        locator=locator,
    )
    return DownloadPlan(game, (artifact,))


def _backend() -> VNMasterFetchBackend:
    backend = object.__new__(VNMasterFetchBackend)
    backend.secrets = SimpleNamespace(f95zone_cookies=None)
    backend._reporter = lambda _message: None
    return backend


def test_resolve_plan_accepts_direct_provider_urls(monkeypatch) -> None:
    monkeypatch.setattr(
        "vnmaster.downloads.fetch_session.build_search_client",
        lambda **_kwargs: httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500)
            )
        ),
    )
    plan = _plan("https://mega.nz/file/abc")

    result = _backend().resolve_plan(plan)

    assert result.ready
    assert result.downloads[0][0].url == "https://mega.nz/file/abc"


def test_resolve_plan_returns_masked_captcha_for_ui_completion(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json={"status": "captcha"})

    monkeypatch.setattr(
        "vnmaster.downloads.fetch_session.build_search_client",
        lambda **_kwargs: httpx.Client(
            transport=httpx.MockTransport(handler)
        ),
    )
    masked = "https://f95zone.to/masked/mega.nz/example"

    result = _backend().resolve_plan(_plan(masked))

    assert not result.ready
    assert not result.errors
    assert result.protected[0].artifact_index == 0
    assert result.protected[0].protected_url == masked


def test_resolve_plan_uses_user_completed_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "vnmaster.downloads.fetch_session.build_search_client",
        lambda **_kwargs: httpx.Client(),
    )
    result = _backend().resolve_plan(
        _plan("https://f95zone.to/masked/mega.nz/example"),
        {0: "https://mega.nz/file/finished"},
    )

    assert result.ready
    assert result.downloads[0][0].url == "https://mega.nz/file/finished"
