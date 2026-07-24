"""Small, injectable adapter around MEGAcmd's ``mega-get`` command."""
from __future__ import annotations

import shutil
# MEGAcmd is invoked with a fixed argument array, never a shell.
import subprocess  # nosec B404
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


class MegaCmdNotFoundError(RuntimeError):
    pass


class MegaDownloadError(RuntimeError):
    pass


def find_mega_get() -> Path:
    if executable := shutil.which("mega-get"):
        return Path(executable)
    app_executable = Path("/Applications/MEGAcmd.app/Contents/MacOS/mega-get")
    if app_executable.is_file():
        return app_executable
    raise MegaCmdNotFoundError(
        "MEGAcmd is not installed or mega-get is not on PATH. "
        "Install MEGAcmd from https://mega.io/cmd and launch it once."
    )


def is_mega_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and host in {
        "mega.nz",
        "www.mega.nz",
        "mega.co.nz",
        "www.mega.co.nz",
    }


def download_mega(
    url: str,
    destination: Path,
    *,
    executable: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[Path]:
    if not is_mega_url(url):
        raise MegaDownloadError("Expected an HTTPS mega.nz public link")
    executable = executable or find_mega_get()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise MegaDownloadError(f"Download staging directory is not empty: {destination}")

    result = runner(
        [str(executable), url.strip(), str(destination)],
        check=False,
    )
    if result.returncode != 0:
        raise MegaDownloadError(
            f"mega-get failed with exit status {result.returncode}"
        )
    downloaded = sorted(destination.iterdir())
    if not downloaded:
        raise MegaDownloadError("mega-get reported success but downloaded no files")
    return downloaded
