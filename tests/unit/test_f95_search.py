"""Tests for the F95Zone latest_alpha JSON search client.

These use httpx.MockTransport — no live network. The endpoint returns
{"status": "ok", "msg": {"data": [<entries>, ...]}}.
"""
from __future__ import annotations


import httpx
import pytest

from vnmaster.f95_search import (
    SEARCH_ENDPOINT,
    build_search_client,
    clean_thread_title,
    search_f95zone,
)


def _ok_response(entries: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "msg": {
                "data": entries,
                "pagination": {"total": len(entries), "page": 1},
                "count": len(entries),
            },
        },
    )


def test_search_endpoint_is_latest_data_php() -> None:
    """Regression: previous implementations scraped /search/?q=... which
    hit XenForo's anti-bot gate. The correct endpoint is the latest_alpha
    JSON API used by F95Zone's own Latest Updates browse page.
    """
    assert SEARCH_ENDPOINT.endswith("/sam/latest_alpha/latest_data.php")


def test_search_parses_thread_id_and_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response([{
            "thread_id": 93340,
            "title": "Eternum",
            "creator": "Caribdis",
            "version": "v0.9.5 Public",
            "likes": 6410,
        }])

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        hits = search_f95zone("eternum", client=client)
    assert len(hits) == 1
    assert hits[0].thread_id == 93340
    assert hits[0].title == "Eternum"
    assert hits[0].url == "https://f95zone.to/threads/.93340/"
    assert hits[0].creator == "Caribdis"
    assert hits[0].version == "v0.9.5 Public"
    assert hits[0].likes == 6410


def test_search_sends_correct_query_params() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return _ok_response([])

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        search_f95zone("Touch of Magic", client=client)

    url = captured["url"]
    # The latest_data.php query string carries cmd, cat, search, page.
    assert "cmd=list" in url
    assert "cat=games" in url
    # "Touch of Magic" should be URL-encoded somehow
    assert "Touch" in url and "Magic" in url
    assert "page=1" in url


def test_search_respects_max_hits() -> None:
    many = [
        {"thread_id": i, "title": f"Game {i}"}
        for i in range(20)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(many)

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        hits = search_f95zone("game", client=client, max_hits=5)
    assert len(hits) == 5


def test_search_drops_entries_missing_thread_id_or_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response([
            {"thread_id": 1, "title": "Real Game"},
            {"thread_id": None, "title": "Missing ID"},
            {"thread_id": 2, "title": ""},  # empty title
            {"title": "No Thread ID"},  # missing key
            {"thread_id": 3, "title": "Another Real"},
        ])

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        hits = search_f95zone("anything", client=client)
    ids = [h.thread_id for h in hits]
    assert ids == [1, 3]


def test_search_empty_query_returns_empty_without_request() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return _ok_response([])

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        assert search_f95zone("", client=client) == []
        assert search_f95zone("   ", client=client) == []
    assert called["n"] == 0


def test_search_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="oops")

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            search_f95zone("anything", client=client)


def test_search_retries_on_429_then_succeeds(monkeypatch) -> None:
    """Regression: F95Zone rate-limits at 429. We must back off and retry
    rather than dropping the entry to NONE."""
    import vnmaster.f95_search as f95mod
    sleeps: list[float] = []
    monkeypatch.setattr(f95mod.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                429, text="rate limited",
                headers={"retry-after": "0"},  # zero so the test is fast
            )
        return _ok_response([{"thread_id": 42, "title": "Eternum"}])

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        hits = search_f95zone("eternum", client=client)
    assert len(hits) == 1
    assert hits[0].thread_id == 42
    assert calls["n"] == 3  # two 429s then a 200
    assert len(sleeps) == 2  # two backoff sleeps


def test_search_gives_up_after_max_retries_on_persistent_429(monkeypatch) -> None:
    import vnmaster.f95_search as f95mod
    monkeypatch.setattr(f95mod.time, "sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="still rate limited",
                               headers={"retry-after": "0"})

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            search_f95zone("anything", client=client, max_retries=2)


def test_search_returns_empty_when_api_reports_non_ok_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "error", "msg": "rate limited"}
        )

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        assert search_f95zone("anything", client=client) == []


def test_clean_thread_title_strips_brackets() -> None:
    assert clean_thread_title("Eternum [v0.7.5] [Caribdis]") == "Eternum"
    assert clean_thread_title("[REN'PY] Eternum [v0.7] [Caribdis]") == "Eternum"
    assert clean_thread_title("Game Name") == "Game Name"
    assert clean_thread_title("Game Name - Subtitle [Complete] [Dev]") == "Game Name - Subtitle"


def test_build_search_client_sends_cookie_header_when_provided() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("cookie", ""))
        return _ok_response([])

    transport = httpx.MockTransport(handler)
    cookies = "xf_user=abc; xf_session=def"
    with build_search_client(transport=transport, cookie_header=cookies) as client:
        search_f95zone("eternum", client=client)
    assert captured == [cookies]


def test_build_search_client_never_sends_f95_cookie_to_another_origin() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.host] = request.headers.get("cookie", "")
        return _ok_response([])

    transport = httpx.MockTransport(handler)
    cookies = "xf_user=abc; xf_session=def"
    with build_search_client(transport=transport, cookie_header=cookies) as client:
        client.get(SEARCH_ENDPOINT)
        client.get("https://api.f95checker.dev/full/123")

    assert captured["f95zone.to"] == cookies
    assert captured["api.f95checker.dev"] == ""


def test_build_search_client_does_not_forward_cookie_on_cross_origin_redirect() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.host] = request.headers.get("cookie", "")
        if request.url.host == "f95zone.to":
            return httpx.Response(
                302,
                headers={"location": "https://api.f95checker.dev/full/123"},
            )
        return _ok_response([])

    transport = httpx.MockTransport(handler)
    with build_search_client(
        transport=transport,
        cookie_header="xf_user=abc; xf_session=def",
    ) as client:
        client.get(SEARCH_ENDPOINT)

    assert "xf_user=abc" in captured["f95zone.to"]
    assert captured["api.f95checker.dev"] == ""


def test_build_search_client_without_cookies_uses_consent_cookie_only() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["cookie"] = request.headers.get("cookie", "")
        return _ok_response([])

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        search_f95zone("eternum", client=client)
    assert "xf_user" in captured["cookie"]


def test_build_search_client_sets_xhr_referer_headers() -> None:
    """The latest_alpha endpoint expects X-Requested-With and the
    /sam/latest_alpha/ Referer to look like a real AJAX call."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["xhr"] = request.headers.get("x-requested-with", "")
        captured["referer"] = request.headers.get("referer", "")
        return _ok_response([])

    transport = httpx.MockTransport(handler)
    with build_search_client(transport=transport) as client:
        search_f95zone("anything", client=client)
    assert captured["xhr"] == "XMLHttpRequest"
    assert "latest_alpha" in captured["referer"]
