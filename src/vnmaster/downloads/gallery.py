"""Adapter for public file hosts supported by gallery-dl."""
from __future__ import annotations

# The gallery-dl module is invoked with a fixed argument array, never a shell.
from dataclasses import dataclass
import json
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_SUPPORTED_HOSTS = {
    "gofile.io",
    "www.gofile.io",
    "mixdrop.ag",
    "www.mixdrop.ag",
    "mixdrop.bz",
    "www.mixdrop.bz",
    "mixdrop.com",
    "www.mixdrop.com",
    "mixdrop.net",
    "www.mixdrop.net",
    "mixdrop.top",
    "www.mixdrop.top",
    "m1xdrop.ag",
    "www.m1xdrop.ag",
}


class GalleryDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class GalleryCandidate:
    """Inert metadata for one file in a provider container."""

    candidate_id: str
    index: int
    filename: str
    size: int | None

    def prompt_value(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "filename": self.filename,
            "size_bytes": self.size,
        }


def is_gallery_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() in _SUPPORTED_HOSTS


def inspect_gallery(
    url: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[GalleryCandidate, ...]:
    """Enumerate a provider container without downloading its payloads."""
    if not is_gallery_url(url):
        raise GalleryDownloadError("Expected a supported public GoFile or MixDrop URL")
    result = runner(
        [
            sys.executable,
            "-m",
            "gallery_dl",
            "--config-ignore",
            "--no-input",
            "--dump-json",
            url.strip(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check_gallery_result(result, url)
    try:
        payload: Any = json.loads(result.stdout or "")
    except (TypeError, ValueError) as exc:
        raise GalleryDownloadError(
            "gallery-dl returned invalid container metadata"
        ) from exc
    if not isinstance(payload, list):
        raise GalleryDownloadError("gallery-dl returned unexpected container metadata")

    candidates: list[GalleryCandidate] = []
    seen_indexes: set[int] = set()
    for message in payload:
        if not isinstance(message, list) or len(message) < 3:
            continue
        metadata = message[2]
        if not isinstance(metadata, dict):
            continue
        candidate = _candidate_from_metadata(metadata)
        if candidate is None:
            continue
        if candidate.index in seen_indexes:
            raise GalleryDownloadError(
                "gallery-dl returned duplicate container indexes"
            )
        seen_indexes.add(candidate.index)
        candidates.append(candidate)
    if not candidates:
        raise GalleryDownloadError("gallery-dl reported an empty provider container")
    return tuple(sorted(candidates, key=lambda item: item.index))


def download_gallery(
    url: str,
    destination: Path,
    *,
    selection: tuple[GalleryCandidate, ...] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[Path]:
    if not is_gallery_url(url):
        raise GalleryDownloadError("Expected a supported public GoFile or MixDrop URL")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise GalleryDownloadError(
            f"Download staging directory is not empty: {destination}"
        )

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--config-ignore",
        "--no-input",
        "--no-mtime",
        "--directory",
        str(destination),
    ]
    if selection is not None:
        if not selection:
            raise GalleryDownloadError("No provider payload was selected")
        indexes = [candidate.index for candidate in selection]
        if len(indexes) != len(set(indexes)) or any(index < 1 for index in indexes):
            raise GalleryDownloadError("Provider payload selection is invalid")
        command.extend(("--range", ",".join(str(index) for index in sorted(indexes))))
    command.append(url.strip())
    result = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    _check_gallery_result(result, url)

    downloaded = sorted(
        path
        for path in destination.rglob("*")
        if path.is_file() and not path.name.endswith((".part", ".tmp"))
    )
    if not downloaded:
        raise GalleryDownloadError("gallery-dl reported success but downloaded no files")
    if selection is not None:
        expected = sorted(candidate.filename.casefold() for candidate in selection)
        actual = sorted(path.name.casefold() for path in downloaded)
        if actual != expected:
            raise GalleryDownloadError(
                "Provider contents changed after selection; refusing to use "
                "unexpected files"
            )
    return downloaded


def _candidate_from_metadata(metadata: dict[object, object]) -> GalleryCandidate | None:
    raw_index = metadata.get("num")
    raw_filename = metadata.get("filename")
    raw_extension = metadata.get("extension")
    raw_size = metadata.get("size")
    if not isinstance(raw_index, int) or raw_index < 1:
        return None
    if not isinstance(raw_filename, str) or not raw_filename.strip():
        return None
    filename = raw_filename.strip()
    if isinstance(raw_extension, str) and raw_extension.strip():
        suffix = f".{raw_extension.strip()}"
        if not filename.casefold().endswith(suffix.casefold()):
            filename = f"{filename}{suffix}"
    size = raw_size if isinstance(raw_size, int) and raw_size >= 0 else None
    return GalleryCandidate(
        candidate_id=f"payload-{raw_index:04d}",
        index=raw_index,
        filename=Path(filename).name,
        size=size,
    )


def _check_gallery_result(
    result: subprocess.CompletedProcess[str], url: str
) -> None:
    if result.returncode == 0:
        return
    detail = _last_error_line(result.stderr or result.stdout or "")
    if "mixdrop" in url.casefold() and "NoneType" in detail:
        detail = (
            "MixDrop changed to a browser reCAPTCHA ticket flow that "
            "gallery-dl cannot currently resolve unattended"
        )
    message = f"gallery-dl failed with exit status {result.returncode}"
    raise GalleryDownloadError(f"{message}: {detail}" if detail else message)


def _last_error_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:500] if lines else ""
