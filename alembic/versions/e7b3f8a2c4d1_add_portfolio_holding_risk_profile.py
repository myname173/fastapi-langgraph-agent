"""Add portfolio, holding, and risk_profile tables.

Revision ID: e7b3f8a2c4d1
Revises: d4a2b9c1e5f7
Create Date: 2026-07-30 00:00:00.000000

Creates three tables for the chat stock-advisor module:
- ``portfolio``: a user's investment portfolio (watch/hold book).
- ``holding``: a single position inside a portfolio (FK → portfolio.id).
- ``risk_profile``: a user's investment risk profile (PK = user_id).

All tables are opt-in via feature flags (ENABLE_PORTFOLIO / ENABLE_RISK_PROFILE)
and are inert unless explicitly enabled.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b3f8a2c4d1"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "d4a2b9c1e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # -- Portfolio -------------------------------------------------------
    op.create_table(
        "portfolio",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("risk_tolerance", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_portfolio_user_id"), "portfolio", ["user_id"], unique=False)

    # -- Holding (FK → portfolio.id) ------------------------------------
    op.create_table(
        "holding",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("portfolio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("symbol", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("cost_price", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_holding_portfolio_id"), "holding", ["portfolio_id"], unique=False)
    op.create_index(op.f("ix_holding_symbol"), "holding", ["symbol"], unique=False)

    # -- Risk profile (PK = user_id) ------------------------------------
    op.create_table(
        "risk_profile",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("risk_tolerance", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("investment_goals", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("horizon", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("monthly_invest", sa.Float(), nullable=False),
        sa.Column("experience", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("risk_profile")
    op.drop_index(op.f("ix_holding_symbol"), table_name="holding")
    op.drop_index(op.f("ix_holding_portfolio_id"), table_name="holding")
    op.drop_table("holding")
    op.drop_index(op.f("ix_portfolio_user_id"), table_name="portfolio")
    op.drop_table("portfolio")
