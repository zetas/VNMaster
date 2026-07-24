"""F95 game resolution and structured download metadata access."""
from __future__ import annotations

import json
import re
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

from vnmaster.downloads.models import (
    DownloadGroup,
    DownloadMirror,
    ForumThread,
    ThreadInfo,
)
from vnmaster.f95_search import F95SearchHit, clean_thread_title, search_f95zone


F95_ORIGIN = "https://f95zone.to"
F95_INDEXER_FULL = "https://api.f95checker.dev/full/{thread_id}"
_THREAD_ID_RE = re.compile(
    r"f95zone\.to/threads/(?:[^/?#]*\.)?(\d+)(?:[/?#]|$)",
    re.I,
)
_XPATH_LINK_RE = re.compile(
    r"^//a\[starts-with\(@href,['\"](?P<prefix>https?://[^'\"]+)['\"]\)\]"
    r"\[(?P<index>\d+)\]$"
)
_POST_ID_RE = re.compile(r"/(?:post-|posts/)(\d+)(?:/|$)", re.I)
_F95_TITLE_PREFIX_RE = re.compile(
    r"""
    ^
    (?:
        (?:
            vn|adv|rpg|sim|slg|req|mod|game|comic|animation
            |ren['’]?py|rpgm|unity|html|qsp|flash|java|rags
            |wolf\s*rpg|unreal(?:\s+engine)?
            |completed|abandoned|onhold|on\s+hold
        )
        \s+
    )+
    """,
    re.I | re.X,
)


class GameResolutionError(ValueError):
    pass


class AmbiguousGameError(GameResolutionError):
    def __init__(self, query: str, hits: list[F95SearchHit]) -> None:
        self.query = query
        self.hits = hits
        choices = ", ".join(_hit_label(hit) for hit in hits[:5])
        super().__init__(f"Ambiguous game name {query!r}; candidates: {choices}")


class ThreadMetadataError(RuntimeError):
    pass


def extract_thread_id(value: str) -> int | None:
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    match = _THREAD_ID_RE.search(stripped)
    return int(match.group(1)) if match else None


def _hit_label(hit: F95SearchHit) -> str:
    details = [
        detail
        for detail in (
            hit.version,
            f"by {hit.creator}" if hit.creator else None,
        )
        if detail
    ]
    suffix = f" [{' · '.join(details)}]" if details else ""
    return f"{hit.title}{suffix} (#{hit.thread_id})"


def resolve_game(value: str, *, client: httpx.Client) -> F95SearchHit:
    """Resolve an F95 URL or a sufficiently unambiguous title."""
    if thread_id := extract_thread_id(value):
        return F95SearchHit(
            title=f"thread #{thread_id}",
            thread_id=thread_id,
            url=f"{F95_ORIGIN}/threads/.{thread_id}/",
        )

    hits = search_f95zone(value, client=client, max_hits=10)
    if not hits:
        # Completed games eventually disappear from the Latest Updates index.
        # Fall back to XenForo's full-forum search when authenticated cookies
        # are available, while filtering obvious translation/mod threads.
        forum_hits = [
            F95SearchHit(thread.title, thread.thread_id, thread.url)
            for thread in search_forum_threads(value, client=client)
            if not _looks_like_addon_thread(thread)
        ]
        query_slug = _slugify(value)
        slug_matches = [
            hit for hit in forum_hits if query_slug and query_slug in _thread_slug(hit.url)
        ]
        if len(slug_matches) == 1:
            return slug_matches[0]
        hits = slug_matches or forum_hits
        if not hits:
            raise GameResolutionError(f"No F95 game found for {value!r}")

    wanted = _normalize_game_name(value)
    exact = [hit for hit in hits if _normalize_game_name(hit.title) == wanted]
    if len(exact) == 1:
        return exact[0]

    ranked = process.extract(
        wanted,
        {hit.thread_id: _normalize_game_name(hit.title) for hit in hits},
        scorer=fuzz.WRatio,
        limit=2,
    )
    if ranked:
        _title, score, thread_id = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if score >= 90 and score - runner_up >= 10:
            return next(hit for hit in hits if hit.thread_id == thread_id)
    raise AmbiguousGameError(value, hits)


def _normalize_game_name(value: str) -> str:
    """Reduce user input and decorated F95 titles to a comparable game name."""
    cleaned = clean_thread_title(value)
    cleaned = _F95_TITLE_PREFIX_RE.sub("", cleaned)
    return "".join(character for character in cleaned.casefold() if character.isalnum())


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _thread_slug(url: str) -> str:
    match = re.search(r"/threads/([^/.]+)", url)
    return match.group(1).casefold() if match else ""


def _looks_like_addon_thread(thread: ForumThread) -> bool:
    """Exclude clearly labelled mod/translation results from game resolution."""
    title = thread.title.strip().casefold()
    return title.startswith(("mod ", "translation ", "others "))


def fetch_thread_info(thread_id: int, *, client: httpx.Client) -> ThreadInfo:
    response = client.get(
        F95_INDEXER_FULL.format(thread_id=thread_id),
        params={"ts": "0"},
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("INDEX_ERROR"):
        raise ThreadMetadataError(
            f"F95Checker indexer could not load thread #{thread_id}: "
            f"{payload.get('INDEX_ERROR')}"
        )

    raw_downloads = payload.get("downloads") or []
    if isinstance(raw_downloads, str):
        try:
            raw_downloads = json.loads(raw_downloads)
        except json.JSONDecodeError as exc:
            raise ThreadMetadataError(
                f"Thread #{thread_id} returned malformed download metadata"
            ) from exc

    groups: list[DownloadGroup] = []
    for raw_group in raw_downloads:
        if not isinstance(raw_group, (list, tuple)) or len(raw_group) != 2:
            continue
        name, raw_mirrors = raw_group
        mirrors: list[DownloadMirror] = []
        if isinstance(raw_mirrors, list):
            for raw_mirror in raw_mirrors:
                if not isinstance(raw_mirror, (list, tuple)) or len(raw_mirror) != 2:
                    continue
                mirror_name, locator = raw_mirror
                if mirror_name and locator:
                    mirrors.append(DownloadMirror(str(mirror_name), str(locator)))
        groups.append(DownloadGroup(str(name or ""), tuple(mirrors)))

    expanded_groups = _expand_extra_post_attachments(
        tuple(groups), thread_url=f"{F95_ORIGIN}/threads/.{thread_id}/", client=client
    )
    info = ThreadInfo(
        thread_id=thread_id,
        title=str(payload.get("name") or f"thread #{thread_id}"),
        version=(str(payload["version"]) if payload.get("version") else None),
        thread_type=_optional_int(payload.get("type")),
        url=f"{F95_ORIGIN}/threads/.{thread_id}/",
        downloads=expanded_groups,
    )
    if info.downloads:
        return info
    scraped = _scrape_thread_download_groups(info.url, client=client)
    if not scraped:
        return info
    return ThreadInfo(
        thread_id=info.thread_id,
        title=info.title,
        version=info.version,
        thread_type=info.thread_type,
        url=info.url,
        downloads=scraped,
    )


def _expand_extra_post_attachments(
    groups: tuple[DownloadGroup, ...],
    *,
    thread_url: str,
    client: httpx.Client,
) -> tuple[DownloadGroup, ...]:
    """Turn Extras links to specific F95 posts into attachment download groups."""
    expanded: list[DownloadGroup] = []
    for group in groups:
        if group.name.strip().casefold() not in {"extra", "extras"}:
            expanded.append(group)
            continue
        for mirror in group.mirrors:
            if not _POST_ID_RE.search(mirror.locator):
                expanded.append(DownloadGroup(mirror.name, (mirror,)))
                continue
            attachments = _post_attachments(
                mirror.locator, thread_url=thread_url, client=client
            )
            for index, attachment in enumerate(attachments, start=1):
                suffix = f" — {_attachment_filename(attachment.locator)}"
                name = mirror.name if len(attachments) == 1 else f"{mirror.name}{suffix}"
                expanded.append(DownloadGroup(name, (attachment,)))
    return tuple(expanded)


def _post_attachments(
    post_url: str, *, thread_url: str, client: httpx.Client
) -> tuple[DownloadMirror, ...]:
    match = _POST_ID_RE.search(post_url)
    if match is None:
        return ()
    post_id = match.group(1)
    response = client.get(
        urljoin(thread_url, post_url),
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    article = soup.select_one(
        f'article.message[data-content="post-{post_id}"], article#js-post-{post_id}'
    )
    if article is None:
        return ()

    found: dict[str, DownloadMirror] = {}
    for anchor in article.select("a[href]"):
        href = urljoin(str(response.url), str(anchor.get("href") or ""))
        parsed = urlsplit(href)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https":
            continue
        if host != "attachments.f95zone.to" and not (
            host == "f95zone.to" and "/attachments/" in parsed.path
        ):
            continue
        found.setdefault(href, DownloadMirror("F95 ATTACHMENT", href))
    return tuple(found.values())


def _attachment_filename(url: str) -> str:
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return re.sub(r"^\d+_", "", filename) or "attachment"


def _scrape_thread_download_groups(
    thread_url: str, *, client: httpx.Client
) -> tuple[DownloadGroup, ...]:
    """Recover hand-authored download rows omitted by the structured index."""
    response = client.get(
        thread_url, headers={"Accept": "text/html,application/xhtml+xml"}
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    post = soup.select_one("article.message-threadStarterPost")
    if post is None:
        return ()

    found: dict[str, DownloadMirror] = {}
    for text_node in post.find_all(string=re.compile(r"\bdownloads?\b", re.I)):
        container = text_node.find_parent(["div", "p", "li", "td"])
        if container is None:
            continue
        # Avoid treating a whole long post as one download row.
        if len(container.get_text(" ", strip=True)) > 500:
            continue
        for anchor in container.select("a[href]"):
            href = urljoin(thread_url, str(anchor.get("href") or ""))
            if not _looks_like_external_download(href):
                continue
            found.setdefault(
                href,
                DownloadMirror(_download_host_name(href, anchor.get_text(" ", strip=True)), href),
            )
    if not found:
        return ()
    return (DownloadGroup("Thread download links", tuple(found.values())),)


def _looks_like_external_download(url: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host:
        return False
    if host in {"docs.google.com", "discord.com", "discord.gg"}:
        return False
    return host != "f95zone.to" or "/masked/" in parsed.path or "/attachments/" in parsed.path


def _download_host_name(url: str, label: str) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "download").casefold()
    known = {
        "mega.nz": "MEGA",
        "www.mega.nz": "MEGA",
        "drive.google.com": "GOOGLE DRIVE",
        "pixeldrain.com": "PIXELDRAIN",
        "www.pixeldrain.com": "PIXELDRAIN",
    }
    if host == "f95zone.to" and "/masked/" in url:
        masked_host = url.split("/masked/", 1)[1].split("/", 1)[0]
        return masked_host.upper()
    return known.get(host, label.strip() or host.upper())


def search_forum_threads(query: str, *, client: httpx.Client) -> list[ForumThread]:
    """Use XenForo's authenticated search to find mod threads."""
    landing = client.get(
        f"{F95_ORIGIN}/search/", headers={"Accept": "text/html,application/xhtml+xml"}
    )
    landing.raise_for_status()
    soup = BeautifulSoup(landing.text, "html.parser")
    token = soup.select_one('form[action="/search/search"] input[name="_xfToken"]')
    if token is None or not token.get("value"):
        raise ThreadMetadataError("F95 search page did not contain an authentication token")

    response = client.post(
        f"{F95_ORIGIN}/search/search",
        data={"keywords": query, "order": "relevance", "_xfToken": token["value"]},
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    response.raise_for_status()
    results = BeautifulSoup(response.text, "html.parser")

    found: dict[int, ForumThread] = {}
    for anchor in results.select("h3.contentRow-title a[href]"):
        href = urljoin(F95_ORIGIN, str(anchor.get("href") or ""))
        thread_id = extract_thread_id(href)
        # Preserve the real whitespace in XenForo's label-append spans. Using
        # a separator here inserts bogus spaces around highlighted letters.
        title = " ".join(anchor.get_text().split())
        if thread_id is None or not title:
            continue
        found.setdefault(thread_id, ForumThread(thread_id, title, href))
    return list(found.values())


def resolve_redacted_locator(
    locator: str, *, thread_url: str, client: httpx.Client
) -> str:
    """Resolve the indexer's redacted XPath locator against an authenticated page.

    F95's masked-link page normally resolves through an AJAX request. The site
    may conditionally require a CAPTCHA; in that case the masked URL is returned
    unchanged so the CLI can hand the challenge to the user's browser.
    """
    if not locator.startswith("//"):
        if _is_masked_f95_url(locator):
            return _resolve_masked_url(locator, client=client)
        return locator
    match = _XPATH_LINK_RE.match(locator)
    if match is None:
        raise ThreadMetadataError(f"Unsupported redacted link selector: {locator}")

    response = client.get(thread_url, headers={"Accept": "text/html,application/xhtml+xml"})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    post = soup.select_one("article.message-threadStarterPost")
    if post is None:
        raise ThreadMetadataError("F95 thread starter post was not found")
    prefix = match.group("prefix")
    matching = [
        str(anchor["href"])
        for anchor in post.select("a[href]")
        if str(anchor.get("href", "")).startswith(prefix)
    ]
    index = int(match.group("index")) - 1
    if index < 0 or index >= len(matching):
        raise ThreadMetadataError(
            f"Download selector expected link {index + 1} for {prefix}, "
            f"but found {len(matching)}"
        )
    return matching[index]


def _is_masked_f95_url(value: str) -> bool:
    return value.casefold().startswith(f"{F95_ORIGIN}/masked/")


def _resolve_masked_url(locator: str, *, client: httpx.Client) -> str:
    response = client.post(
        locator,
        data={"xhr": "1", "download": "1"},
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": locator,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ThreadMetadataError("F95 masked-link response was not JSON") from exc
    status = payload.get("status")
    if status == "ok" and isinstance(payload.get("msg"), str):
        return str(payload["msg"])
    if status == "captcha":
        return locator
    raise ThreadMetadataError(
        f"F95 masked-link resolution failed: {payload.get('msg') or status or 'unknown'}"
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
