"""add game install state

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "game_installs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("f95_thread_id", sa.Integer(), nullable=False),
        sa.Column("game_title", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("install_path", sa.String(), nullable=False),
        sa.Column("thread_url", sa.String(), nullable=False),
        sa.Column("platform", sa.String(), nullable=True),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("artifacts_json", sa.Text(), nullable=False),
        sa.Column("archive_hashes_json", sa.Text(), nullable=False),
        sa.Column("verification_json", sa.Text(), nullable=False),
        sa.Column("renpy_game_dir", sa.String(), nullable=True),
        sa.Column("urm_path", sa.String(), nullable=True),
        sa.Column("installed_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("last_rebuilt_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("install_path"),
    )
    op.create_index(
        op.f("ix_game_installs_f95_thread_id"),
        "game_installs",
        ["f95_thread_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_game_installs_f95_thread_id"), table_name="game_installs")
    op.drop_table("game_installs")
