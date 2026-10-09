"""Add sdlc_task.coder_model column.

Revision ID: a1c8f3e7d920
Revises: b7c4e9f1a2d3
Create Date: 2026-10-07 00:00:00.000000

Records which coder model a run was launched with.

Why this column exists: the task-management list has a 「模型」 column, but the
frontend read it from ``plan[0].model`` — a key the planner never writes — so
the column rendered ``-`` for every row. The requested coder model was already
known at task-creation time (``SDLCTaskRequest.coder_model``) but was only kept
in the LangGraph checkpoint, not in ``sdlc_task``.

Nullable: pre-existing rows have no recorded model and stay NULL (the UI shows
``-`` for them rather than inventing a value).
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c8f3e7d920"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "b7c4e9f1a2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sdlc_task", sa.Column("coder_model", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sdlc_task", "coder_model")
