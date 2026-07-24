"""VikingFile adapter using its public file-check API before download."""
from __future__ import annotations

import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from vnmaster.downloads.http import (
    AddressResolver,
    DirectDownloadError,
    request_public_https,
    stream_public_https,
)


_FILE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class VikingFileDownloadError(RuntimeError):
    pass


def vikingfile_hash(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {
        "vikingfile.com",
        "www.vikingfile.com",
        "vik1ngfile.site",
        "www.vik1ngfile.site",
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "f":
        return None
    return parts[1] if _FILE_HASH_RE.fullmatch(parts[1]) else None


def is_vikingfile_url(url: str) -> bool:
    return vikingfile_hash(url) is not None


def download_vikingfile(
    url: str,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    resolver: AddressResolver = socket.getaddrinfo,
) -> list[Path]:
    file_hash = vikingfile_hash(url)
    if file_hash is None:
        raise VikingFileDownloadError("Expected a public VikingFile URL")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise VikingFileDownloadError(
            f"Download staging directory is not empty: {destination}"
        )

    own_client = client is None
    active_client = client
    if active_client is None:
        active_client = httpx.Client(
            timeout=httpx.Timeout(30.0, read=None),
            trust_env=False,
        )
    try:
        info = _file_info(file_hash, active_client, resolver=resolver)
        filename = Path(str(info.get("name") or f"{file_hash}.download")).name
        if filename in {"", ".", ".."}:
            raise VikingFileDownloadError("VikingFile returned an unsafe filename")
        expected_size = info.get("size")
        with stream_public_https(
            active_client,
            url.strip(),
            resolver=resolver,
        ) as (response, _final_url):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "text/html" in content_type:
                page = response.read().decode("utf-8", errors="replace").casefold()
                if "turnstile" in page or "captcha-download" in page:
                    size = f" ({expected_size} bytes)" if isinstance(expected_size, int) else ""
                    raise VikingFileDownloadError(
                        f"VikingFile confirms {filename!r}{size} exists, but its free "
                        "download requires browser Turnstile confirmation"
                    )
                raise VikingFileDownloadError(
                    "VikingFile returned an unsupported download landing page"
                )

            output = destination / filename
            partial = destination / f".{filename}.part"
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    handle.write(chunk)
            if partial.stat().st_size == 0:
                raise VikingFileDownloadError("VikingFile returned an empty file")
            if isinstance(expected_size, int) and partial.stat().st_size != expected_size:
                raise VikingFileDownloadError(
                    f"VikingFile download size mismatch for {filename!r}"
                )
            partial.replace(output)
            return [output]
    except DirectDownloadError as exc:
        raise VikingFileDownloadError(f"VikingFile download blocked: {exc}") from exc
    except httpx.HTTPError as exc:
        raise VikingFileDownloadError(f"VikingFile download failed: {exc}") from exc
    finally:
        if own_client:
            active_client.close()


def _file_info(
    file_hash: str,
    client: httpx.Client,
    *,
    resolver: AddressResolver,
) -> dict[str, object]:
    response = request_public_https(
        client,
        "POST",
        "https://vikingfile.com/api/check-file",
        resolver=resolver,
        data={"hash": file_hash},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise VikingFileDownloadError(
            "VikingFile returned malformed file metadata"
        ) from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise VikingFileDownloadError("VikingFile returned malformed file metadata")
    info: dict[str, object] = payload[0]
    if info.get("exist") is not True:
        raise VikingFileDownloadError("VikingFile reports that this file has expired")
    return info
