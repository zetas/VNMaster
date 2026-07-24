"""DataNodes public-link adapter with explicit expiry/CAPTCHA detection."""
from __future__ import annotations

import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from vnmaster.downloads.http import (
    AddressResolver,
    DirectDownloadError,
    stream_public_https,
)


_FILE_CODE_RE = re.compile(r"^[A-Za-z0-9]{12}$")


class DataNodesDownloadError(RuntimeError):
    pass


def datanodes_file_code(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {
        "datanodes.to",
        "www.datanodes.to",
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts and _FILE_CODE_RE.fullmatch(parts[0]) else None


def is_datanodes_url(url: str) -> bool:
    return datanodes_file_code(url) is not None


def download_datanodes(
    url: str,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    resolver: AddressResolver = socket.getaddrinfo,
) -> list[Path]:
    if datanodes_file_code(url) is None:
        raise DataNodesDownloadError("Expected a public DataNodes file URL")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise DataNodesDownloadError(
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
        with stream_public_https(
            active_client,
            url.strip(),
            resolver=resolver,
        ) as (response, final_url):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "text/html" in content_type:
                page = response.read().decode("utf-8", errors="replace")
                if "Files here expire fast" in page or "from=deadfile" in page:
                    raise DataNodesDownloadError("DataNodes reports that this file has expired")
                if "recaptcha" in page.casefold():
                    raise DataNodesDownloadError(
                        "DataNodes requires a browser reCAPTCHA for this public file"
                    )
                raise DataNodesDownloadError(
                    "DataNodes returned an unsupported download landing page"
                )

            filename = _filename(final_url, url)
            output = destination / filename
            partial = destination / f".{filename}.part"
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    handle.write(chunk)
            if partial.stat().st_size == 0:
                raise DataNodesDownloadError("DataNodes returned an empty file")
            partial.replace(output)
            return [output]
    except DirectDownloadError as exc:
        raise DataNodesDownloadError(f"DataNodes download blocked: {exc}") from exc
    except httpx.HTTPError as exc:
        raise DataNodesDownloadError(f"DataNodes download failed: {exc}") from exc
    finally:
        if own_client:
            active_client.close()


def _filename(final_url: str, original_url: str) -> str:
    candidate = (
        Path(urlsplit(final_url).path).name
        or Path(urlsplit(original_url).path).name
    )
    filename = Path(candidate).name
    if filename in {"", ".", "..", "download"}:
        raise DataNodesDownloadError("DataNodes did not provide a safe filename")
    return filename
