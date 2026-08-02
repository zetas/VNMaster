from __future__ import annotations

import json

import httpx
import pytest

from vnmaster.downloads.f95 import (
    AmbiguousGameError,
    extract_thread_id,
    fetch_starter_post_text,
    fetch_thread_info,
    resolve_game,
    resolve_redacted_locator,
    search_forum_threads,
)
from vnmaster.f95_search import F95SearchHit


def test_extract_thread_id_accepts_canonical_and_short_urls() -> None:
    assert extract_thread_id("https://f95zone.to/threads/game-name.12345/") == 12345
    assert extract_thread_id("https://f95zone.to/threads/12345") == 12345
    assert extract_thread_id("https://f95zone.to/threads/.12345/") == 12345
    assert extract_thread_id("12345") == 12345
    assert extract_thread_id("Eternum") is None


def test_starter_post_text_redacts_visible_urls() -> None:
    html = """
    <article class="message-threadStarterPost">
      <div class="bbWrapper">
        Part 1 <a href="https://mega.nz/file/secret">https://mega.nz/file/secret</a>
        <script>https://example.invalid/script</script>
      </div>
    </article>
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        text = fetch_starter_post_text("https://f95zone.to/threads/.1/", client=client)
    assert text == "Part 1\n[URL REDACTED]"


def test_resolve_game_prefers_exact_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "msg": {
                    "data": [
                        {"thread_id": 1, "title": "Eternum", "version": "0.9"},
                        {"thread_id": 2, "title": "Zoolux Eternum", "version": "0.2"},
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hit = resolve_game("Eternum", client=client)
    assert hit.thread_id == 1


def test_resolve_game_normalizes_decorated_forum_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "msg": {
                    "data": [
                        {
                            "thread_id": 161093,
                            "title": (
                                "VN Ren'Py New Year's Day(e) "
                                "[Ch. 5 v0.5.0] [Jonesy]"
                            ),
                        },
                        {
                            "thread_id": 146412,
                            "title": (
                                "VN Ren'Py Completed Christmas Eve "
                                "[v2.0.0] [Jonesy]"
                            ),
                        },
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hit = resolve_game("new years daye", client=client)
    assert hit.thread_id == 161093


def test_resolve_game_reports_ambiguous_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "msg": {
                    "data": [
                        {"thread_id": 1, "title": "Magic Days"},
                        {"thread_id": 2, "title": "Days of Magic"},
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AmbiguousGameError, match="candidates"):
            resolve_game("Magic", client=client)


def test_ambiguous_error_includes_version_creator_and_thread_id() -> None:
    error = AmbiguousGameError(
        "Artemis",
        [
            F95SearchHit(
                "Artemis",
                77680,
                "https://f95zone.to/threads/77680",
                creator="digi.B",
                version="v0.7.1",
            )
        ],
    )
    assert "v0.7.1" in str(error)
    assert "digi.B" in str(error)
    assert "#77680" in str(error)


def test_resolve_game_falls_back_to_forum_for_completed_game() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("latest_data.php"):
            return httpx.Response(
                200,
                json={"status": "ok", "msg": {"data": [], "count": 0}},
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    '<form action="/search/search">'
                    '<input name="_xfToken" value="token"></form>'
                ),
            )
        return httpx.Response(
            200,
            text=(
                '<h3 class="contentRow-title"><a href="/threads/'
                'a-petal-among-thorns-v6-0-2-re-lockheart.87472/">'
                "VN Ren'Py Completed A Petal Among Thorns</a></h3>"
                '<h3 class="contentRow-title"><a href="/threads/'
                'a-petal-among-thorns-french-translation.106195/">'
                "Mod Ren'Py A Petal Among Thorns French Translation</a></h3>"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hit = resolve_game("A Petal among Thorns", client=client)
    assert hit.thread_id == 87472


def test_fetch_thread_info_decodes_download_groups() -> None:
    downloads = [["Mac", [["MEGA", "https://f95zone.to/masked/mega.nz/x"]]]]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/full/42"
        return httpx.Response(
            200,
            json={
                "name": "A Game",
                "version": "v1.2",
                "type": "5",
                "downloads": json.dumps(downloads),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        info = fetch_thread_info(42, client=client)
    assert info.title == "A Game"
    assert info.version == "v1.2"
    assert info.thread_type == 5
    assert info.downloads[0].name == "Mac"
    assert info.downloads[0].mirrors[0].name == "MEGA"


def test_fetch_thread_info_recovers_download_row_when_index_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/full/42":
            return httpx.Response(
                200,
                json={
                    "name": "A Translation",
                    "version": "v1",
                    "downloads": "[]",
                },
            )
        return httpx.Response(
            200,
            text=(
                '<article class="message-threadStarterPost">'
                '<div>Download: '
                '<a href="https://drive.google.com/file/d/abc/view">Google Drive</a>, '
                '<a href="https://mega.nz/file/abc#key">MEGA</a>'
                "</div></article>"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        info = fetch_thread_info(42, client=client)
    assert info.downloads[0].name == "Thread download links"
    assert [mirror.name for mirror in info.downloads[0].mirrors] == [
        "GOOGLE DRIVE",
        "MEGA",
    ]


def test_fetch_thread_info_uses_mod_attachment_but_ignores_screenshots() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/full/99":
            return httpx.Response(
                200,
                json={"name": "A Game Walkthrough Mod", "version": "v1", "downloads": []},
            )
        return httpx.Response(
            200,
            text=(
                '<article class="message-threadStarterPost"><div class="bbWrapper">'
                '<a href="https://attachments.f95zone.to/2026/07/1_preview.jpg">pic</a>'
                '<a href="https://attachments.f95zone.to/2026/07/2_mod-v1.2.zip">mod</a>'
                "</div></article>"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        info = fetch_thread_info(99, client=client)
    assert len(info.downloads) == 1
    assert [mirror.locator for mirror in info.downloads[0].mirrors] == [
        "https://attachments.f95zone.to/2026/07/2_mod-v1.2.zip"
    ]


def test_fetch_thread_info_follows_linked_mod_post_and_author_download_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/full/99":
            return httpx.Response(
                200,
                json={"name": "A Game Walkthrough Mod", "version": "v1", "downloads": []},
            )
        if request.url.path == "/threads/.99/":
            return httpx.Response(
                200,
                text=(
                    '<article class="message-threadStarterPost"><div class="bbWrapper">'
                    'Download information moved <a href="/threads/mod.99/post-123">here</a>.'
                    "</div></article>"
                ),
            )
        if request.url.path == "/threads/mod.99/post-123":
            return httpx.Response(
                200,
                text=(
                    '<article class="message" data-content="post-123">'
                    'Use <a href="https://mods.example/current/">the direct link</a>.'
                    "</article>"
                ),
            )
        assert request.url == "https://mods.example/current/"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=(
                "<main><article><p>Download WT Mod v1.2 "
                '<a href="https://vikingfile.com/f/current">VikingFile</a> '
                '<a href="https://www.mediafire.com/file/mod-v1.2.zip/file">MediaFire</a>'
                "</p></article></main>"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        info = fetch_thread_info(99, client=client)
    assert info.downloads[0].name.startswith("Download WT Mod v1.2")
    assert [mirror.name for mirror in info.downloads[0].mirrors] == [
        "VikingFile",
        "MediaFire",
    ]


def test_fetch_thread_info_expands_extras_post_attachment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/full/42":
            return httpx.Response(
                200,
                json={
                    "name": "A Game",
                    "version": "v2",
                    "downloads": json.dumps(
                        [
                            ["Mac", [["MEGA", "https://mega.nz/file/game#key"]]],
                            [
                                "Extras",
                                [
                                    [
                                        "Game patch",
                                        "https://f95zone.to/threads/a-game.42/post-123",
                                    ],
                                    [
                                        "Translation",
                                        "https://f95zone.to/threads/translations.9/post-456",
                                    ],
                                ],
                            ],
                        ]
                    ),
                },
            )
        if "post-123" in str(request.url):
            return httpx.Response(
                200,
                text=(
                    '<article class="message" id="js-post-123" '
                    'data-content="post-123">'
                    '<a href="https://attachments.f95zone.to/2026/03/'
                    '123_patch.rar">patch.rar</a>'
                    '<a href="https://attachments.f95zone.to/2026/03/'
                    '123_patch.rar">duplicate</a>'
                    "</article>"
                ),
            )
        return httpx.Response(
            200,
            text='<article class="message" data-content="post-456"></article>',
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        info = fetch_thread_info(42, client=client)
    assert [group.name for group in info.downloads] == ["Mac", "Game patch"]
    patch = info.downloads[1].mirrors[0]
    assert patch.name == "F95 ATTACHMENT"
    assert patch.locator.endswith("/123_patch.rar")


def test_search_forum_threads_posts_token_and_dedupes() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    '<form action="/search/search">'
                    '<input name="_xfToken" value="token"></form>'
                ),
            )
        return httpx.Response(
            200,
            text=(
                '<h3 class="contentRow-title">'
                '<a href="/threads/a-game-multimod.99/">A Game Multi-Mod</a></h3>'
                '<h3 class="contentRow-title">'
                '<a href="/threads/a-game-multimod.99/">duplicate</a></h3>'
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hits = search_forum_threads("A Game mod", client=client)
    assert calls == [("GET", "/search/"), ("POST", "/search/search")]
    assert [(hit.thread_id, hit.title) for hit in hits] == [(99, "A Game Multi-Mod")]


def test_search_forum_threads_preserves_spacing_around_highlights() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    '<form action="/search/search">'
                    '<input name="_xfToken" value="token"></form>'
                ),
            )
        return httpx.Response(
            200,
            text=(
                '<h3 class="contentRow-title"><a href="/threads/a-game.99/">'
                '<span>Mod</span><span class="label-append">&nbsp;</span>'
                'A Petal <em class="textHighlight">A</em>mong Thorns'
                "</a></h3>"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hits = search_forum_threads("A Petal among Thorns", client=client)
    assert hits[0].title == "Mod A Petal Among Thorns"


def test_resolve_redacted_locator_uses_thread_starter_post() -> None:
    selector = "//a[starts-with(@href,'https://mega.nz/')][2]"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                '<article class="message-threadStarterPost">'
                '<a href="https://mega.nz/file/first">MEGA</a>'
                '<a href="https://mega.nz/file/second">MEGA</a>'
                "</article>"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        resolved = resolve_redacted_locator(
            selector, thread_url="https://f95zone.to/threads/.1/", client=client
        )
    assert resolved == "https://mega.nz/file/second"


def test_resolve_redacted_locator_leaves_masked_url_for_browser() -> None:
    locator = "https://f95zone.to/masked/mega.nz/token"
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        return httpx.Response(200, json={"status": "captcha"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert (
            resolve_redacted_locator(locator, thread_url="unused", client=client)
            == locator
        )


def test_resolve_redacted_locator_uses_masked_ajax_redirect() -> None:
    locator = "https://f95zone.to/masked/pixeldrain.com/token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.content == b"xhr=1&download=1"
        return httpx.Response(
            200,
            json={"status": "ok", "msg": "https://pixeldrain.com/u/abc123"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert resolve_redacted_locator(
            locator, thread_url="unused", client=client
        ) == "https://pixeldrain.com/u/abc123"
