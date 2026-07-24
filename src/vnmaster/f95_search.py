"""Search F95Zone by title via the latest_alpha JSON endpoint.

F95Zone's `/sam/latest_alpha/latest_data.php` is the same JSON API that
powers their Latest Updates browse page. It accepts a `search` query
parameter and returns clean structured data — no XenForo form, no CSRF,
no HTML parsing, no anti-scraping gates.

Endpoint:  GET https://f95zone.to/sam/latest_alpha/latest_data.php
Query:     cmd=list&cat=games&search=<term>[&page=N]
Response:  {"status": "ok", "msg": {"data": [<game>, ...],
                                     "pagination": {...},
                                     "count": N}}

Each game record contains: thread_id, title, creator, version, views,
likes, prefixes (thread tag IDs), tags (numeric IDs), rating, cover URL.

Authentication: works without F95Zone login cookies for the search query
itself, but it's polite to pass them (and required for some category
filters). VNMaster's wizard sends them when available.
"""
from __future__ import annotations

import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from vnmaster.logging_setup import get_logger

log = get_logger(__name__)


SEARCH_ENDPOINT = "https://f95zone.to/sam/latest_alpha/latest_data.php"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# F95Zone's adult-content gate sets this cookie when accepted. Pre-set so
# unauthenticated requests don't bounce to the consent page.
_ADULT_CONSENT_COOKIE = {"xf_user": "0"}

# Strip XenForo bracket tags from result titles: "Eternum [v0.7] [Dev]" → "Eternum".
_BRACKET_TAG_RE = re.compile(r"\s*\[[^\]]*\]\s*")


@dataclass(frozen=True)
class F95SearchHit:
    title: str
    thread_id: int
    url: str
    creator: str | None = None
    version: str | None = None
    likes: int | None = None


def build_search_client(
    *,
    transport: httpx.BaseTransport | None = None,
    cookie_header: str | None = None,
) -> httpx.Client:
    """Construct a configured httpx.Client for repeated F95Zone API calls.

    Authentication cookies are stored in httpx's domain-scoped cookie jar.
    They must never be installed as a default ``Cookie`` header because this
    client also follows redirects and may be reused for public metadata calls
    on other origins.

    Caller is responsible for closing the client (use as a context manager).
    """
    headers = {
        "User-Agent": _DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://f95zone.to/sam/latest_alpha/",
        "X-Requested-With": "XMLHttpRequest",
    }
    cookies = _f95zone_cookie_jar(cookie_header)
    return httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers=headers,
        cookies=cookies,
        transport=transport,
    )


def _f95zone_cookie_jar(cookie_header: str | None) -> httpx.Cookies:
    """Parse a browser Cookie header into cookies scoped to f95zone.to."""
    values: dict[str, str] = {}
    if cookie_header:
        for part in cookie_header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                values[name] = value
    if not values:
        values.update(_ADULT_CONSENT_COOKIE)

    cookies = httpx.Cookies()
    for name, value in values.items():
        cookies.set(name, value, domain="f95zone.to", path="/")
    return cookies


def search_f95zone(
    query: str,
    *,
    client: httpx.Client | None = None,
    max_hits: int = 10,
    category: str = "games",
    max_retries: int = 3,
) -> list[F95SearchHit]:
    """Search F95Zone for `query` and return up to `max_hits` results.

    Empty `query` returns []. On HTTP 429 (rate limit) we honor the
    Retry-After header and back off with jitter, retrying up to
    `max_retries` times before giving up. Other HTTP errors and
    malformed JSON are re-raised so the caller can fall back to a
    manual search URL.
    """
    query = query.strip()
    if not query:
        return []

    own_client = client is None
    active_client = client or build_search_client()
    try:
        resp = _get_with_retry(
            active_client,
            SEARCH_ENDPOINT,
            params={"cmd": "list", "cat": category, "search": query, "page": "1"},
            max_retries=max_retries,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "ok":
            log.warning("F95Zone API returned status=%r for %r", body.get("status"), query)
            return []
        data = (body.get("msg") or {}).get("data") or []
        hits: list[F95SearchHit] = []
        for entry in data[:max_hits]:
            thread_id = entry.get("thread_id")
            title = entry.get("title")
            if thread_id is None or not title:
                continue
            hits.append(
                F95SearchHit(
                    title=str(title),
                    thread_id=int(thread_id),
                    url=f"https://f95zone.to/threads/.{int(thread_id)}/",
                    creator=entry.get("creator"),
                    version=entry.get("version"),
                    likes=entry.get("likes"),
                )
            )
        log.debug("F95Zone search %r → %d hits", query, len(hits))
        return hits
    finally:
        if own_client:
            active_client.close()


def _get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: Mapping[str, str],
    max_retries: int,
) -> httpx.Response:
    """GET with 429-aware retry. Honors Retry-After when provided."""
    attempt = 0
    while True:
        resp = client.get(url, params=params)
        if resp.status_code != 429:
            return resp
        attempt += 1
        if attempt > max_retries:
            return resp  # caller will see 429 and raise
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            wait = float(retry_after)
        else:
            # Retry jitter is intentionally non-cryptographic.
            wait = (2 ** attempt) + random.uniform(0, 1.0)  # nosec B311
        log.warning(
            "F95Zone returned 429; backing off %.1fs (attempt %d/%d)",
            wait, attempt, max_retries,
        )
        time.sleep(wait)


def search_with_backoff(
    query: str,
    *,
    client: httpx.Client,
    delay_seconds: float = 3.0,
) -> list[F95SearchHit]:
    """Wrapper that sleeps after each call to space out repeated queries.

    F95Zone enforces a separate (stricter) rate limit on the JSON API
    endpoint than on HTML thread pages. Observed: bursting 67 queries at
    0.4s intervals → "temporarily blocked" message that lasts hours;
    pacing at 1.5s → 429s partway through; 3s → reliably clean.

    Yes, 3s × 67 entries = ~3.5 minutes wall-clock, which is slow. But
    a single round of getting blocked costs the user a much longer wait
    for the block to clear, so the slower pace is the right default.
    """
    try:
        return search_f95zone(query, client=client)
    finally:
        time.sleep(delay_seconds)


def clean_thread_title(title: str) -> str:
    """Strip XenForo bracket tags from a thread title.

    "[REN'PY] Eternum [v0.7] [Caribdis]" → "Eternum"

    The latest_data.php endpoint already returns clean titles (just "Eternum"),
    but we keep this function for the wizard scoring path so it can handle
    titles from either source defensively.
    """
    cleaned = _BRACKET_TAG_RE.sub(" ", title).strip()
    return " ".join(cleaned.split())
