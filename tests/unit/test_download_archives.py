from __future__ import annotations

import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from vnmaster.downloads.archives import (
    UnsafeArchiveError,
    _validate_zip,
    extract_archive,
    unpack_payload,
)


def test_zip_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../../escaped.txt", "nope")
    with pytest.raises(UnsafeArchiveError, match="unsafe path"):
        _validate_zip(archive)


def test_zip_extraction_enforces_actual_written_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "underreported.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("game/data.bin", b"1234567")
    destination = tmp_path / "out"
    destination.mkdir()

    monkeypatch.setattr("vnmaster.downloads.archives._validate_zip", lambda _path: 1)
    monkeypatch.setattr("vnmaster.downloads.archives.MAX_UNPACKED_BYTES", 4)
    monkeypatch.setattr("vnmaster.downloads.archives.MIN_FREE_BYTES_AFTER_EXTRACT", 0)

    with pytest.raises(UnsafeArchiveError, match="safety limit"):
        extract_archive(archive, destination)
    assert not (destination / "game" / "data.bin").exists()


def test_zip_extraction_preserves_executable_permissions(tmp_path: Path) -> None:
    archive = tmp_path / "game.zip"
    executable = zipfile.ZipInfo("Game.app/Contents/MacOS/launcher")
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(executable, b"launcher")
    destination = tmp_path / "out"
    destination.mkdir()

    extract_archive(archive, destination)

    launcher = destination / "Game.app" / "Contents" / "MacOS" / "launcher"
    assert launcher.read_bytes() == b"launcher"
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755


def test_plain_download_payload_is_copied(tmp_path: Path) -> None:
    source = tmp_path / "Game.app"
    source.mkdir()
    (source / "launcher").write_text("game")
    destination = tmp_path / "unpacked"
    unpack_payload([source], destination)
    assert (destination / "Game.app" / "launcher").read_text() == "game"


def test_rar_uses_bsdtar_after_validating_member_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "patch.rar"
    archive.write_bytes(b"rar")
    destination = tmp_path / "out"
    destination.mkdir()
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "vnmaster.downloads.archives.shutil.which",
        lambda name: "/fake/bsdtar" if name == "bsdtar" else None,
    )

    def runner(args, **kwargs):
        calls.append(args)
        if "-tf" in args:
            stdout = "game/script.rpy\ngame/script.rpyc\n"
        elif "-tvf" in args:
            stdout = (
                "-rw-r--r--  0 user group 12 Jan 01 00:00 game/script.rpy\n"
                "-rw-r--r--  0 user group 24 Jan 01 00:00 game/script.rpyc\n"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    extract_archive(archive, destination, runner=runner)
    assert calls == [
        ["/fake/bsdtar", "-tf", str(archive)],
        ["/fake/bsdtar", "-tvf", str(archive)],
        ["/fake/bsdtar", "-xf", str(archive), "-C", str(destination)],
    ]


def test_seven_zip_rejects_unsafe_symbolic_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "patch.7z"
    archive.write_bytes(b"7z")
    destination = tmp_path / "out"
    destination.mkdir()

    monkeypatch.setattr(
        "vnmaster.downloads.archives.shutil.which",
        lambda name: "/fake/7zz" if name == "7zz" else None,
    )

    listing = """----------
Path = game/link
Size = 12
Symbolic Link = ../../../outside

"""

    def runner(args, **kwargs):
        stdout = listing if "l" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    with pytest.raises(UnsafeArchiveError, match="unsafe symbolic link"):
        extract_archive(archive, destination, runner=runner)


def test_seven_zip_rejects_oversized_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vnmaster.downloads.archives import MAX_UNPACKED_BYTES

    archive = tmp_path / "patch.7z"
    archive.write_bytes(b"7z")
    destination = tmp_path / "out"
    destination.mkdir()

    monkeypatch.setattr(
        "vnmaster.downloads.archives.shutil.which",
        lambda name: "/fake/7zz" if name == "7zz" else None,
    )
    listing = f"""----------
Path = game/huge.bin
Size = {MAX_UNPACKED_BYTES + 1}

"""

    def runner(args, **kwargs):
        stdout = listing if "l" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    with pytest.raises(UnsafeArchiveError, match="safety limit"):
        extract_archive(archive, destination, runner=runner)
