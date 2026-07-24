"""Streaming downloader for public PixelDrain file links."""
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


_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class PixelDrainDownloadError(RuntimeError):
    pass


def pixeldrain_file_id(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {
        "pixeldrain.com",
        "www.pixeldrain.com",
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "u":
        file_id = parts[1]
    elif len(parts) >= 3 and parts[:2] == ["api", "file"]:
        file_id = parts[2]
    else:
        return None
    return file_id if _FILE_ID_RE.fullmatch(file_id) else None


def is_pixeldrain_url(url: str) -> bool:
    return pixeldrain_file_id(url) is not None


def download_pixeldrain(
    url: str,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    resolver: AddressResolver = socket.getaddrinfo,
) -> list[Path]:
    file_id = pixeldrain_file_id(url)
    if file_id is None:
        raise PixelDrainDownloadError("Expected an HTTPS pixeldrain.com file link")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise PixelDrainDownloadError(
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
        info_response = request_public_https(
            active_client,
            "GET",
            f"https://pixeldrain.com/api/file/{file_id}/info",
            resolver=resolver,
        )
        info_response.raise_for_status()
        info = info_response.json()
        if not info.get("success"):
            raise PixelDrainDownloadError(
                str(info.get("message") or "PixelDrain file is unavailable")
            )
        filename = Path(str(info.get("name") or f"{file_id}.download")).name
        if filename in {"", ".", ".."}:
            filename = f"{file_id}.download"
        expected_size = info.get("size")
        output = destination / filename
        partial = destination / f".{filename}.part"

        with stream_public_https(
            active_client,
            f"https://pixeldrain.com/api/file/{file_id}?download",
            resolver=resolver,
        ) as (response, _final_url):
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    handle.write(chunk)
        if isinstance(expected_size, int) and partial.stat().st_size != expected_size:
            raise PixelDrainDownloadError(
                f"PixelDrain download size mismatch for {filename!r}"
            )
        partial.replace(output)
        return [output]
    except DirectDownloadError as exc:
        raise PixelDrainDownloadError(f"PixelDrain download blocked: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = str(exc.response.json().get("message") or "")
        except (ValueError, AttributeError):
            pass
        message = f"PixelDrain download failed with HTTP {exc.response.status_code}"
        raise PixelDrainDownloadError(
            f"{message}: {detail}" if detail else message
        ) from exc
    finally:
        if own_client:
            active_client.close()
