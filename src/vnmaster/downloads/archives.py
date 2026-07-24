"""Safe-ish archive validation and staged extraction.

Extraction always targets a fresh staging directory. ZIP and tar member paths
are validated before an external extractor sees them; 7z/rar names are listed
and checked first, and 7-Zip is never given the switch that permits absolute
paths.
"""
from __future__ import annotations

import posixpath
import shutil
import stat
# External extractors are invoked with fixed executables and argument arrays;
# shell=True is never used.
import subprocess  # nosec B404
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable


MAX_ARCHIVE_MEMBERS = 200_000
MAX_UNPACKED_BYTES = 100 * 1024 * 1024 * 1024
MIN_FREE_BYTES_AFTER_EXTRACT = 2 * 1024 * 1024 * 1024
MAX_SYMLINK_TARGET_BYTES = 4096


class UnsafeArchiveError(RuntimeError):
    pass


class UnsupportedArchiveError(RuntimeError):
    pass


def unpack_payload(
    downloaded: list[Path],
    destination: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for item in downloaded:
        if item.is_dir():
            shutil.copytree(item, destination / item.name)
        elif _archive_kind(item) is not None:
            extract_archive(item, destination, runner=runner)
        else:
            shutil.copy2(item, destination / item.name)


def extract_archive(
    archive: Path,
    destination: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    kind = _archive_kind(archive)
    if kind == "zip":
        unpacked_bytes = _validate_zip(archive)
        _validate_extraction_capacity(unpacked_bytes, destination)
        _extract_zip_streaming(archive, destination)
        return
    if kind == "tar":
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            _validate_names([member.name for member in members])
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise UnsafeArchiveError("Archive contains too many files")
            unpacked_bytes = sum(member.size for member in members)
            _validate_extraction_capacity(unpacked_bytes, destination)
            bundle.extractall(destination, filter="data")
        return
    if kind == "rar":
        executable = shutil.which("bsdtar")
        if executable is None:
            raise UnsupportedArchiveError(".rar extraction requires bsdtar")
        listing = runner(
            [executable, "-tf", str(archive)],
            check=False,
            capture_output=True,
            text=True,
        )
        _check_extract_result(listing, archive)
        names = (listing.stdout or "").splitlines()
        _validate_names(names)
        if len(names) > MAX_ARCHIVE_MEMBERS:
            raise UnsafeArchiveError("Archive contains too many files")
        verbose_listing = runner(
            [executable, "-tvf", str(archive)],
            check=False,
            capture_output=True,
            text=True,
        )
        _check_extract_result(verbose_listing, archive)
        unpacked_bytes = _bsdtar_unpacked_bytes(
            verbose_listing.stdout or "",
            names=names,
        )
        _validate_extraction_capacity(unpacked_bytes, destination)
        result = runner(
            [executable, "-xf", str(archive), "-C", str(destination)],
            check=False,
        )
        _check_extract_result(result, archive)
        return
    if kind == "7z":
        executable = shutil.which("7zz") or shutil.which("7z")
        if executable is None:
            raise UnsupportedArchiveError(
                ".7z extraction requires 7zz"
            )
        listing = runner(
            [executable, "l", "-slt", str(archive)],
            check=False,
            capture_output=True,
            text=True,
        )
        _check_extract_result(listing, archive)
        records = _seven_zip_member_records(listing.stdout or "")
        names = [record["Path"] for record in records]
        _validate_names(names)
        if len(names) > MAX_ARCHIVE_MEMBERS:
            raise UnsafeArchiveError("Archive contains too many files")
        unpacked_bytes = _seven_zip_unpacked_bytes(records)
        _validate_seven_zip_links(records)
        _validate_extraction_capacity(unpacked_bytes, destination)
        result = runner(
            [executable, "x", f"-o{destination}", "--", str(archive)],
            check=False,
        )
        _check_extract_result(result, archive)
        return
    raise UnsupportedArchiveError(f"Unsupported archive type: {archive.name}")


def _archive_kind(path: Path) -> str | None:
    lower = path.name.casefold()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz")):
        return "tar"
    if lower.endswith(".7z"):
        return "7z"
    if lower.endswith(".rar"):
        return "rar"
    return None


def _validate_zip(path: Path) -> int:
    with zipfile.ZipFile(path) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise UnsafeArchiveError("Archive contains too many files")
        unpacked_bytes = sum(member.file_size for member in members)
        if unpacked_bytes > MAX_UNPACKED_BYTES:
            raise UnsafeArchiveError("Archive expands beyond the configured safety limit")
        _validate_names([member.filename for member in members])
        for member in members:
            mode = member.external_attr >> 16
            if not stat.S_ISLNK(mode):
                continue
            target = _zip_link_target(bundle, member)
            if not _safe_link_target(member.filename, target):
                raise UnsafeArchiveError(
                    f"Archive contains an unsafe symbolic link: {member.filename}"
                )
        return unpacked_bytes


def _extract_zip_streaming(path: Path, destination: Path) -> None:
    """Extract ZIP members with an enforced counter on bytes actually written."""
    total_written = 0
    with zipfile.ZipFile(path) as bundle:
        for member in bundle.infolist():
            output = _safe_member_output(destination, member.filename)
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            allowed_types = {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}
            if file_type not in allowed_types:
                raise UnsafeArchiveError(
                    f"Archive contains an unsupported special file: {member.filename}"
                )

            if member.is_dir() or file_type == stat.S_IFDIR:
                if output.is_symlink():
                    output.unlink()
                if output.exists() and not output.is_dir():
                    raise UnsafeArchiveError(
                        f"Archive member conflicts with an existing file: {member.filename}"
                    )
                output.mkdir(parents=True, exist_ok=True)
                continue

            output.parent.mkdir(parents=True, exist_ok=True)
            _ensure_parent_within_destination(output, destination)
            if output.is_symlink():
                output.unlink()
            elif output.exists() and output.is_dir():
                raise UnsafeArchiveError(
                    f"Archive member conflicts with an existing directory: {member.filename}"
                )

            if file_type == stat.S_IFLNK:
                target = _zip_link_target(bundle, member)
                total_written += len(target.encode("utf-8", errors="surrogateescape"))
                _validate_streaming_extraction_capacity(
                    total_written,
                    destination,
                )
                if not _safe_link_target(member.filename, target):
                    raise UnsafeArchiveError(
                        f"Archive contains an unsafe symbolic link: {member.filename}"
                    )
                output.unlink(missing_ok=True)
                output.symlink_to(target)
                continue

            try:
                with bundle.open(member) as source, output.open("wb") as handle:
                    while chunk := source.read(1024 * 1024):
                        total_written += len(chunk)
                        _validate_streaming_extraction_capacity(
                            total_written,
                            destination,
                            pending_bytes=len(chunk),
                        )
                        handle.write(chunk)
            except Exception:
                output.unlink(missing_ok=True)
                raise

            permissions = stat.S_IMODE(mode)
            if permissions:
                output.chmod(permissions)


def _zip_link_target(bundle: zipfile.ZipFile, member: zipfile.ZipInfo) -> str:
    with bundle.open(member) as source:
        raw_target = source.read(MAX_SYMLINK_TARGET_BYTES + 1)
    if len(raw_target) > MAX_SYMLINK_TARGET_BYTES:
        raise UnsafeArchiveError(
            f"Archive symbolic link target is too long: {member.filename}"
        )
    return raw_target.decode("utf-8", errors="surrogateescape")


def _validate_names(names: list[str]) -> None:
    for name in names:
        normalized = name.replace("\\", "/")
        member = PurePosixPath(normalized)
        if not member.parts or str(member) == ".":
            raise UnsafeArchiveError("Archive contains an empty path")
        if member.is_absolute() or ".." in member.parts:
            raise UnsafeArchiveError(f"Archive contains an unsafe path: {name}")
        if member.parts and member.parts[0].endswith(":"):
            raise UnsafeArchiveError(f"Archive contains an absolute Windows path: {name}")


def _safe_member_output(destination: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    output = destination.joinpath(*member.parts)
    _ensure_parent_within_destination(output, destination)
    return output


def _ensure_parent_within_destination(output: Path, destination: Path) -> None:
    root = destination.resolve()
    parent = output.parent.resolve()
    if parent != root and root not in parent.parents:
        raise UnsafeArchiveError(
            f"Archive member escapes through a symbolic link: {output.name}"
        )


def _safe_link_target(member_name: str, target: str) -> bool:
    if target.startswith(("/", "\\")):
        return False
    combined = posixpath.normpath(
        posixpath.join(posixpath.dirname(member_name.replace("\\", "/")), target)
    )
    return combined != ".." and not combined.startswith("../")


def _seven_zip_member_records(output: str) -> list[dict[str, str]]:
    """Parse member records after the archive header in ``7z l -slt``."""
    if "----------" in output:
        output = output.split("----------", 1)[1]
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if record:
                records.append(record)
                record = {}
            continue
        key, separator, value = line.partition(" = ")
        if separator:
            record[key] = value
    if record:
        records.append(record)
    return [record for record in records if "Path" in record]


def _seven_zip_member_names(output: str) -> list[str]:
    """Compatibility helper used by callers/tests that only need names."""
    return [record["Path"] for record in _seven_zip_member_records(output)]


def _seven_zip_unpacked_bytes(records: list[dict[str, str]]) -> int:
    total = 0
    for record in records:
        raw_size = record.get("Size")
        if raw_size is None or not raw_size.isdigit():
            raise UnsafeArchiveError(
                f"Could not determine expanded size for {record['Path']}"
            )
        total += int(raw_size)
        if total > MAX_UNPACKED_BYTES:
            raise UnsafeArchiveError("Archive expands beyond the configured safety limit")
    return total


def _validate_seven_zip_links(records: list[dict[str, str]]) -> None:
    for record in records:
        target = record.get("Symbolic Link")
        if target is not None and not _safe_link_target(record["Path"], target):
            raise UnsafeArchiveError(
                f"Archive contains an unsafe symbolic link: {record['Path']}"
            )


def _bsdtar_unpacked_bytes(output: str, *, names: list[str]) -> int:
    """Read sizes and link targets from ``bsdtar -tvf`` output."""
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != len(names):
        raise UnsafeArchiveError("Could not determine expanded size for RAR archive")

    total = 0
    for name, line in zip(names, lines, strict=True):
        fields = line.split(maxsplit=8)
        if len(fields) < 9 or not fields[4].isdigit():
            raise UnsafeArchiveError(f"Could not determine expanded size for {name}")
        total += int(fields[4])
        if total > MAX_UNPACKED_BYTES:
            raise UnsafeArchiveError("Archive expands beyond the configured safety limit")
        if fields[0].startswith("l"):
            _link_name, separator, target = fields[8].rpartition(" -> ")
            if not separator or not _safe_link_target(name, target):
                raise UnsafeArchiveError(
                    f"Archive contains an unsafe symbolic link: {name}"
                )
    return total


def _validate_extraction_capacity(unpacked_bytes: int, destination: Path) -> None:
    if unpacked_bytes > MAX_UNPACKED_BYTES:
        raise UnsafeArchiveError("Archive expands beyond the configured safety limit")
    free_bytes = shutil.disk_usage(destination).free
    usable_bytes = max(0, free_bytes - MIN_FREE_BYTES_AFTER_EXTRACT)
    if unpacked_bytes > usable_bytes:
        raise UnsafeArchiveError(
            "Archive would leave less than 2 GiB of free disk space"
        )


def _validate_streaming_extraction_capacity(
    written_bytes: int,
    destination: Path,
    *,
    pending_bytes: int = 0,
) -> None:
    if written_bytes > MAX_UNPACKED_BYTES:
        raise UnsafeArchiveError("Archive expands beyond the configured safety limit")
    if (
        shutil.disk_usage(destination).free - pending_bytes
        < MIN_FREE_BYTES_AFTER_EXTRACT
    ):
        raise UnsafeArchiveError(
            "Archive extraction reached the 2 GiB free disk safety reserve"
        )


def _check_extract_result(
    result: subprocess.CompletedProcess[str], archive: Path
) -> None:
    if result.returncode != 0:
        raise UnsafeArchiveError(
            f"Extractor failed for {archive.name} with exit status {result.returncode}"
        )
