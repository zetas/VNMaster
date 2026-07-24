"""add daily-check watermark columns and digest_runs.kind

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-01 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('digest_runs') as batch_op:
        batch_op.add_column(
            sa.Column('kind', sa.String(), nullable=False, server_default='weekly')
        )
    with op.batch_alter_table('library_games') as batch_op:
        batch_op.add_column(
            sa.Column('last_daily_notified_version', sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('last_daily_notified_status', sa.String(), nullable=True)
        )
    op.execute(
        "UPDATE library_games SET last_daily_notified_version = latest_upstream_version"
    )
    op.execute(
        "UPDATE library_games SET last_daily_notified_status = status"
    )


def downgrade() -> None:
    with op.batch_alter_table('library_games') as batch_op:
        batch_op.drop_column('last_daily_notified_status')
        batch_op.drop_column('last_daily_notified_version')
    with op.batch_alter_table('digest_runs') as batch_op:
        batch_op.drop_column('kind')
