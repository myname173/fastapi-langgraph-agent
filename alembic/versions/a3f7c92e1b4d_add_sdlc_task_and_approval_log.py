"""Add sdlc_task and approval_log tables.

Revision ID: a3f7c92e1b4d
Revises: b25d38b0cd7c
Create Date: 2026-07-09 10:00:00.000000

H-6 fix: SDLCTask and ApprovalLog are table=True SQLModel models but
the initial migration (b25d38b0cd7c) only created user/session/thread.
Without this migration, runtime INSERTs fail with
"relation does not exist" on fresh databases.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f7c92e1b4d"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "b25d38b0cd7c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sdlc_task",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("thread_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("requirements", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("plan", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sdlc_task_thread_id"), "sdlc_task", ["thread_id"], unique=False)

    op.create_table(
        "approval_log",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("task_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("step", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("action", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("state_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status_before", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status_after", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approval_log_task_id"), "approval_log", ["task_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_approval_log_task_id"), table_name="approval_log")
    op.drop_table("approval_log")
    op.drop_index(op.f("ix_sdlc_task_thread_id"), table_name="sdlc_task")
    op.drop_table("sdlc_task")
