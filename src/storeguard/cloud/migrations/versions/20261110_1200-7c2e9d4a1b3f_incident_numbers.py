"""stable per-store incident numbers

Revision ID: 7c2e9d4a1b3f
Revises: 0d1fe1d49cca
Create Date: 2026-11-10 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '7c2e9d4a1b3f'
down_revision: str | None = '0d1fe1d49cca'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('number', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_events_number'), ['number'], unique=False)
    # Existing incidents: number them in arrival order, per store.
    op.execute(
        """
        UPDATE events SET number = (
            SELECT COUNT(*) FROM events AS e2
            WHERE e2.tenant_id = events.tenant_id
              AND (e2.created_at < events.created_at
                   OR (e2.created_at = events.created_at AND e2.id <= events.id))
        )
        """
    )


def downgrade() -> None:
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_events_number'))
        batch_op.drop_column('number')
