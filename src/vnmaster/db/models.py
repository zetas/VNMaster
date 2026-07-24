"""ORM models for vnmaster.db. Schema mirrors spec §4.4, §4.5, §4.8."""
from __future__ import annotations

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class LibraryGame(Base):
    __tablename__ = "library_games"

    f95_thread_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_title: Mapped[str] = mapped_column(String, nullable=False)
    # Play history (nullable: user may never have played)
    save_dir_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_played_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_played_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    save_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_save_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Currently installed (nullable: may not be on disk now)
    install_path: Mapped[str | None] = mapped_column(String, nullable=True)
    installed_version: Mapped[str | None] = mapped_column(String, nullable=True)
    disk_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # F95Checker mirror
    latest_upstream_version: Mapped[str | None] = mapped_column(String, nullable=True)
    upstream_last_updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_thread_url: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_changelog: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    # 1 on the run a game's notable status (completed/abandoned/on-hold) changed.
    status_changed: Mapped[int] = mapped_column(Integer, default=0)
    developer: Mapped[str | None] = mapped_column(String, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # User reaction state
    hidden: Mapped[int] = mapped_column(Integer, default=0)
    interested: Mapped[int] = mapped_column(Integer, default=0)
    acknowledged_version: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_in_digest_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-game watermark for the nightly (daily) alert: the upstream version and
    # notable status last alerted via a daily run. Baselined on insert so the
    # existing backlog doesn't fire on the first nightly run.
    last_daily_notified_version: Mapped[str | None] = mapped_column(String, nullable=True)
    last_daily_notified_status: Mapped[str | None] = mapped_column(String, nullable=True)
    # Audit
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Pairing(Base):
    __tablename__ = "pairings"

    f95_thread_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    save_dir_name: Mapped[str | None] = mapped_column(String, nullable=True)
    folder_name: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    paired_at: Mapped[int] = mapped_column(Integer, nullable=False)


class GameInstall(Base):
    __tablename__ = "game_installs"
    __table_args__ = (UniqueConstraint("install_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    f95_thread_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    game_title: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    install_path: Mapped[str] = mapped_column(String, nullable=False)
    thread_url: Mapped[str] = mapped_column(String, nullable=False)
    platform: Mapped[str | None] = mapped_column(String, nullable=True)
    host: Mapped[str] = mapped_column(String, nullable=False)
    source_locator: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False)
    archive_hashes_json: Mapped[str] = mapped_column(Text, nullable=False)
    verification_json: Mapped[str] = mapped_column(Text, nullable=False)
    renpy_game_dir: Mapped[str | None] = mapped_column(String, nullable=True)
    urm_path: Mapped[str | None] = mapped_column(String, nullable=True)
    installed_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    last_rebuilt_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ChangelogExtraction(Base):
    __tablename__ = "changelog_extractions"
    __table_args__ = (UniqueConstraint("f95_thread_id", "content_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    f95_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String, nullable=False)
    versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_at: Mapped[int] = mapped_column(Integer, nullable=False)


class DigestRun(Base):
    __tablename__ = "digest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updates_count: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    # 'weekly' or 'daily'. Only 'weekly' runs move the "since last digest" pointer.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="weekly")

    entries: Mapped[list["DigestEntry"]] = relationship(back_populates="run")


class DigestEntry(Base):
    __tablename__ = "digest_entries"
    __table_args__ = (UniqueConstraint("discord_message_id", "embed_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("digest_runs.id"), nullable=False)
    discord_message_id: Mapped[str] = mapped_column(String, nullable=False)
    embed_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # 'update'
    f95_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)

    run: Mapped[DigestRun] = relationship(back_populates="entries")
