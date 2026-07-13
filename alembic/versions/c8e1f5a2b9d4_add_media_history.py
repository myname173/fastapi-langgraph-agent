"""Add media_history table.

Revision ID: c8e1f5a2b9d4
Revises: a3f7c92e1b4d
Create Date: 2026-07-09 23:10:00.000000

L-14 fix: the /api/v1/media endpoints generate images and videos but
leave no audit trail. Without a history table, users cannot review
past generations or diagnose failures, and quota auditing is blind.
This migration creates the ``media_history`` table populated by
``media_history_service`` on every generation outcome (success or
failure).
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e1f5a2b9d4"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "a3f7c92e1b4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "media_history",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("media_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("prompt", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("result_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("task_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("size", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("resolution", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("error_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_media_history_user_id"), "media_history", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_media_history_user_id"), table_name="media_history")
    op.drop_table("media_history")
