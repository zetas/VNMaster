"""Persist download/install state in VNMaster's SQLite database."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy import Engine, select

from vnmaster.db.engine import session_scope
from vnmaster.db.models import GameInstall
from vnmaster.downloads.service import ArtifactExecution, DownloadExecutionResult


class InstallStateError(RuntimeError):
    pass


class InstallStateNotFoundError(InstallStateError):
    pass


class AmbiguousInstallStateError(InstallStateError):
    pass


class _HashDigest(Protocol):
    def update(self, value: bytes) -> None: ...

    def hexdigest(self) -> str: ...


@dataclass(frozen=True)
class InstallState:
    id: int
    f95_thread_id: int
    game_title: str
    version: str | None
    install_path: Path
    thread_url: str
    platform: str | None
    host: str
    source_locator: str
    artifacts: tuple[dict[str, object], ...]
    archive_hashes: dict[str, str]
    verification_checks: tuple[str, ...]
    renpy_game_dir: Path | None
    urm_path: Path | None
    installed_at: int
    updated_at: int
    last_rebuilt_at: int | None


def save_install_state(
    engine: Engine,
    result: DownloadExecutionResult,
    *,
    reporter: Callable[[str], None] = lambda _message: None,
) -> InstallState:
    """Hash preserved payloads and upsert one completed installation."""
    if not result.artifacts:
        raise InstallStateError("Download result has no artifacts")
    game_execution = result.artifacts[0]
    game = game_execution.artifact
    archive_hashes: dict[str, str] = {}
    for execution in result.artifacts:
        for relative in execution.archive_paths:
            reporter(f"Hashing preserved payload: {relative}")
            archive_hashes[str(relative)] = hash_payload(result.final_dir / relative)

    artifacts = tuple(_artifact_payload(execution) for execution in result.artifacts)
    now = int(time.time())
    install_path = str(result.final_dir)
    with session_scope(engine) as session:
        row = session.execute(
            select(GameInstall).where(GameInstall.install_path == install_path)
        ).scalar_one_or_none()
        if row is None:
            row = GameInstall(
                f95_thread_id=game.thread_id,
                game_title=game.title,
                version=game.version,
                install_path=install_path,
                thread_url=game.thread_url,
                platform=game_execution.download.platform,
                host=game_execution.download.host,
                source_locator=game_execution.download.locator,
                artifacts_json="[]",
                archive_hashes_json="{}",
                verification_json="[]",
                installed_at=now,
                updated_at=now,
            )
            session.add(row)
        row.f95_thread_id = game.thread_id
        row.game_title = game.title
        row.version = game.version
        row.thread_url = game.thread_url
        row.platform = game_execution.download.platform
        row.host = game_execution.download.host
        row.source_locator = game_execution.download.locator
        row.artifacts_json = json.dumps(artifacts, ensure_ascii=False)
        row.archive_hashes_json = json.dumps(archive_hashes, ensure_ascii=False)
        row.verification_json = json.dumps(result.verification_checks, ensure_ascii=False)
        row.renpy_game_dir = (
            str(result.renpy_game_dir) if result.renpy_game_dir is not None else None
        )
        row.urm_path = str(result.urm_path) if result.urm_path is not None else None
        row.updated_at = now
        session.flush()
        state = _to_state(row)
    return state


def resolve_install_state(engine: Engine, query: str) -> InstallState:
    """Resolve an install by exact path, thread ID, or normalized title."""
    with session_scope(engine) as session:
        rows = session.execute(select(GameInstall)).scalars().all()
        states = [_to_state(row) for row in rows]

    expanded = Path(query).expanduser()
    path_matches = [
        state
        for state in states
        if state.install_path == expanded or str(state.install_path) == query
    ]
    if len(path_matches) == 1:
        return path_matches[0]

    id_matches = (
        [state for state in states if state.f95_thread_id == int(query)]
        if query.strip().isdigit()
        else []
    )
    if len(id_matches) == 1:
        return id_matches[0]
    if len(id_matches) > 1:
        raise _ambiguous_state(query, id_matches)

    wanted = _normalize_name(query)
    title_matches = [state for state in states if _normalize_name(state.game_title) == wanted]
    if len(title_matches) == 1:
        return title_matches[0]
    if len(title_matches) > 1:
        raise _ambiguous_state(query, title_matches)
    raise InstallStateNotFoundError(f"No recorded game install found for {query!r}")


def list_install_states(engine: Engine) -> tuple[InstallState, ...]:
    with session_scope(engine) as session:
        rows = session.execute(
            select(GameInstall).order_by(GameInstall.game_title, GameInstall.version)
        ).scalars()
        return tuple(_to_state(row) for row in rows)


def mark_rebuilt(
    engine: Engine,
    state: InstallState,
    *,
    verification_checks: tuple[str, ...],
) -> InstallState:
    now = int(time.time())
    with session_scope(engine) as session:
        row = session.get(GameInstall, state.id)
        if row is None:
            raise InstallStateNotFoundError(f"Recorded install #{state.id} no longer exists")
        row.verification_json = json.dumps(verification_checks, ensure_ascii=False)
        row.last_rebuilt_at = now
        row.updated_at = now
        session.flush()
        return _to_state(row)


def _artifact_payload(execution: ArtifactExecution) -> dict[str, object]:
    artifact = execution.artifact
    merge = execution.addon_merge
    return {
        "kind": artifact.kind,
        "title": artifact.title,
        "version": artifact.version,
        "thread_id": artifact.thread_id,
        "thread_url": artifact.thread_url,
        "group_name": execution.download.group_name or artifact.group_name,
        "platform": execution.download.platform or artifact.platform,
        "host": execution.download.host,
        "source_locator": execution.download.locator,
        "output_path": str(execution.output_path),
        "archive_paths": [str(path) for path in execution.archive_paths],
        "installable": merge is not None,
        "merge": (
            {
                "source_path": str(merge.source_path),
                "target_path": str(merge.target_path),
                "files_installed": merge.files_installed,
                "files_overwritten": merge.files_overwritten,
                "readme_path": (str(merge.readme_path) if merge.readme_path is not None else None),
            }
            if merge is not None
            else None
        ),
    }


def hash_payload(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        _hash_file(path, digest)
        return digest.hexdigest()
    if not path.is_dir():
        raise InstallStateError(f"Preserved payload is missing: {path}")
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if child.is_file():
            _hash_file(child, digest)
    return digest.hexdigest()


def _hash_file(path: Path, digest: _HashDigest) -> None:
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)


def _to_state(row: GameInstall) -> InstallState:
    return InstallState(
        id=row.id,
        f95_thread_id=row.f95_thread_id,
        game_title=row.game_title,
        version=row.version,
        install_path=Path(row.install_path),
        thread_url=row.thread_url,
        platform=row.platform,
        host=row.host,
        source_locator=row.source_locator,
        artifacts=tuple(json.loads(row.artifacts_json)),
        archive_hashes=dict(json.loads(row.archive_hashes_json)),
        verification_checks=tuple(json.loads(row.verification_json)),
        renpy_game_dir=Path(row.renpy_game_dir) if row.renpy_game_dir else None,
        urm_path=Path(row.urm_path) if row.urm_path else None,
        installed_at=row.installed_at,
        updated_at=row.updated_at,
        last_rebuilt_at=row.last_rebuilt_at,
    )


def _normalize_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _ambiguous_state(query: str, states: list[InstallState]) -> AmbiguousInstallStateError:
    choices = ", ".join(
        f"{state.game_title} {state.version or ''} ({state.install_path})" for state in states
    )
    return AmbiguousInstallStateError(f"Multiple recorded installs match {query!r}: {choices}")
