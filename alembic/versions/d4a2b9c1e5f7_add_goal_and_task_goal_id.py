"""Add goal table and sdlc_task.goal_id column.

Revision ID: d4a2b9c1e5f7
Revises: c8e1f5a2b9d4
Create Date: 2026-07-26 00:00:00.000000

Creates the ``goal`` table for cross-session long-term objective tracking,
and adds a nullable ``goal_id`` column to ``sdlc_task`` for task-to-goal
association. No foreign key constraint — goal deletion nullifies the
task's goal_id at the application layer, preserving task history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a2b9c1e5f7"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "c8e1f5a2b9d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "goal",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_goal_user_id"), "goal", ["user_id"], unique=False)

    # Add nullable goal_id to sdlc_task (existing rows get NULL — no Goal).
    op.add_column("sdlc_task", sa.Column("goal_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_index(op.f("ix_sdlc_task_goal_id"), "sdlc_task", ["goal_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_sdlc_task_goal_id"), table_name="sdlc_task")
    op.drop_column("sdlc_task", "goal_id")
    op.drop_index(op.f("ix_goal_user_id"), table_name="goal")
    op.drop_table("goal")
