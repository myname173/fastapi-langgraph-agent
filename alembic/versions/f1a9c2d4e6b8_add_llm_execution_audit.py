"""Add content-free LLM execution audit records.

Revision ID: f1a9c2d4e6b8
Revises: e7b3f8a2c4d1
Create Date: 2026-08-03 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

revision: str = "f1a9c2d4e6b8"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "e7b3f8a2c4d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the LLM execution audit table."""
    op.create_table(
        "llm_execution",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("execution_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("agent_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("purpose", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("requested_model", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("effective_model", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("actual_model", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("deployment", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_execution_execution_id"), "llm_execution", ["execution_id"], unique=False)
    op.create_index(op.f("ix_llm_execution_user_id"), "llm_execution", ["user_id"], unique=False)


def downgrade() -> None:
    """Remove the LLM execution audit table."""
    op.drop_index(op.f("ix_llm_execution_user_id"), table_name="llm_execution")
    op.drop_index(op.f("ix_llm_execution_execution_id"), table_name="llm_execution")
    op.drop_table("llm_execution")
