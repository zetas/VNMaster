"""Conservative downloader for direct HTTPS file URLs."""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx


_FILENAME_RE = re.compile(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", re.I)
_MAX_REDIRECTS = 10
SocketAddress = tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]
AddressInfo = tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    SocketAddress,
]
AddressResolver = Callable[..., Iterable[AddressInfo]]
FormData = Mapping[str, str]


class DirectDownloadError(RuntimeError):
    pass


def is_safe_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def download_direct_https(
    url: str,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    resolver: AddressResolver = socket.getaddrinfo,
) -> list[Path]:
    if not is_safe_https_url(url):
        raise DirectDownloadError("Expected a public HTTPS download URL")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise DirectDownloadError(f"Download staging directory is not empty: {destination}")

    own_client = client is None
    if own_client:
        client = httpx.Client(
            timeout=httpx.Timeout(30.0, read=None),
            trust_env=False,
        )
    active_client = client
    if active_client is None:
        raise DirectDownloadError("Could not initialize the HTTP client")
    try:
        current_url = url.strip()
        with stream_public_https(
            active_client,
            current_url,
            resolver=resolver,
        ) as (response, final_url):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "text/html" in content_type or "application/json" in content_type:
                host = urlsplit(final_url).hostname or "Download host"
                raise DirectDownloadError(
                    f"{host} returned a web page instead of a file; "
                    "this host needs a dedicated adapter"
                )
            filename = _response_filename(response, final_url)
            output = destination / filename
            partial = destination / f".{filename}.part"
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    handle.write(chunk)
            if partial.stat().st_size == 0:
                raise DirectDownloadError("Download returned an empty file")
            partial.replace(output)
            return [output]
    except httpx.HTTPError as exc:
        raise DirectDownloadError(f"Direct download failed: {exc}") from exc
    finally:
        if own_client:
            active_client.close()


@contextmanager
def stream_public_https(
    client: httpx.Client,
    url: str,
    *,
    resolver: AddressResolver = socket.getaddrinfo,
    method: str = "GET",
    data: FormData | None = None,
) -> Iterator[tuple[httpx.Response, str]]:
    """Stream a public HTTPS URL while pinning every hop to a vetted address.

    The URL sent to httpx contains the selected numeric address, while the
    original hostname remains in both the HTTP Host header and TLS SNI. This
    prevents a second DNS lookup from changing the destination after validation.
    """
    current_url = url.strip()
    current_method = method.upper()
    current_data = data
    for redirect_count in range(_MAX_REDIRECTS + 1):
        pinned_url, host_header, sni_hostname = _pinned_request_target(
            current_url,
            resolver=resolver,
        )
        with client.stream(
            current_method,
            pinned_url,
            headers={"Host": host_header},
            data=current_data,
            extensions={"sni_hostname": sni_hostname},
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise DirectDownloadError(
                        "Download returned a redirect without a destination"
                    )
                if redirect_count == _MAX_REDIRECTS:
                    raise DirectDownloadError("Download redirected too many times")
                current_url = urljoin(current_url, location)
                if response.status_code in {301, 302, 303} and current_method != "HEAD":
                    current_method = "GET"
                    current_data = None
                continue
            yield response, current_url
            return
    raise DirectDownloadError("Download redirected too many times")


def request_public_https(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    resolver: AddressResolver = socket.getaddrinfo,
    data: FormData | None = None,
) -> httpx.Response:
    """Return a fully-read response using the pinned public-HTTPS transport."""
    with stream_public_https(
        client,
        url,
        resolver=resolver,
        method=method,
        data=data,
    ) as (response, _final_url):
        response.read()
        return response


def _pinned_request_target(
    url: str,
    *,
    resolver: AddressResolver,
) -> tuple[str, str, str]:
    if not is_safe_https_url(url):
        raise DirectDownloadError("Download redirected to an unsafe URL")
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise DirectDownloadError("Download URL has no host")
    try:
        addresses = resolver(
            host,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise DirectDownloadError(f"Could not resolve download host {host}") from exc

    resolved = tuple(
        dict.fromkeys(
            ipaddress.ip_address(address[4][0])
            for address in addresses
            if len(address) >= 5 and address[4]
        )
    )
    if not resolved:
        raise DirectDownloadError(f"Download host {host} resolved to no addresses")
    if any(not address.is_global for address in resolved):
        raise DirectDownloadError(
            f"Download host {host} resolves to a non-public network address"
        )

    # Prefer IPv4 when both families are available. It avoids selecting an
    # unreachable IPv6 address without performing a second DNS lookup.
    selected = min(resolved, key=lambda address: address.version)
    selected_host = (
        f"[{selected.compressed}]"
        if selected.version == 6
        else selected.compressed
    )
    port = parsed.port
    pinned_authority = f"{selected_host}:{port}" if port is not None else selected_host

    original_host = f"[{host}]" if ":" in host else host
    host_header = (
        f"{original_host}:{port}"
        if port is not None and port != 443
        else original_host
    )
    pinned_url = parsed._replace(netloc=pinned_authority).geturl()
    return pinned_url, host_header, host


def _response_filename(response: httpx.Response, source_url: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = _FILENAME_RE.search(disposition)
    candidate = (
        unquote(match.group(1)).strip()
        if match
        else Path(urlsplit(source_url).path).name
    )
    filename = Path(candidate).name
    if filename in {"", ".", ".."}:
        raise DirectDownloadError("Download response did not provide a safe filename")
    return filename
