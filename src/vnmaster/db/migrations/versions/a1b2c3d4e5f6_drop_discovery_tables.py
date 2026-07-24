"""drop discovery tables and discovery_count column

Revision ID: a1b2c3d4e5f6
Revises: 9fface4a8c7e
Create Date: 2026-06-08 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9fface4a8c7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('discovery_suggestions')
    op.drop_table('discovery_state')
    # Drop discovery_count from digest_runs via batch alter (required for SQLite).
    with op.batch_alter_table('digest_runs') as batch_op:
        batch_op.drop_column('discovery_count')


def downgrade() -> None:
    # Recreate discovery_count on digest_runs.
    with op.batch_alter_table('digest_runs') as batch_op:
        batch_op.add_column(sa.Column('discovery_count', sa.Integer(), nullable=False, server_default='0'))
    # Recreate discovery tables.
    op.create_table(
        'discovery_state',
        sa.Column('f95_thread_id', sa.Integer(), nullable=False),
        sa.Column('hidden', sa.Integer(), nullable=False),
        sa.Column('skip_until', sa.Integer(), nullable=True),
        sa.Column('interested', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('f95_thread_id'),
    )
    op.create_table(
        'discovery_suggestions',
        sa.Column('f95_thread_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('developer', sa.String(), nullable=True),
        sa.Column('posted_at', sa.Integer(), nullable=False),
        sa.Column('likes', sa.Integer(), nullable=True),
        sa.Column('tags_json', sa.Text(), nullable=True),
        sa.Column('short_description', sa.Text(), nullable=True),
        sa.Column('image_url', sa.String(), nullable=True),
        sa.Column('first_seen_at', sa.Integer(), nullable=False),
        sa.Column('last_seen_in_digest_at', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('f95_thread_id'),
    )
