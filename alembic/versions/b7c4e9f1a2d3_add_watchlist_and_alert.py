"""Add watchlist_item and alert tables.

Revision ID: b7c4e9f1a2d3
Revises: f1a9c2d4e6b8
Create Date: 2026-10-05 00:00:00.000000

Creates the two remaining P2 stock-advisor tables that had models but no
schema migration:

- ``watchlist_item``: a user's curated symbol tracker (自选股).
- ``alert``: a user's price-breakout trigger (价格预警), one-shot semantics.

Both tables are opt-in via feature flags (ENABLE_WATCHLIST / ENABLE_ALERT)
and are inert unless explicitly enabled. Column types mirror the SQLModel
definitions in ``app/models/watchlist.py`` and ``app/models/alert.py``
(``AlertCondition`` is a ``str`` enum, stored as a plain string — same
convention as ``portfolio.risk_tolerance``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c4e9f1a2d3"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "f1a9c2d4e6b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # -- Watchlist item (自选股) -----------------------------------------
    op.create_table(
        "watchlist_item",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_watchlist_item_user_id"), "watchlist_item", ["user_id"], unique=False)
    op.create_index(op.f("ix_watchlist_item_symbol"), "watchlist_item", ["symbol"], unique=False)

    # -- Alert (价格预警) -------------------------------------------------
    op.create_table(
        "alert",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("condition", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("fired_at", sa.DateTime(), nullable=True),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_alert_user_id"), "alert", ["user_id"], unique=False)
    op.create_index(op.f("ix_alert_symbol"), "alert", ["symbol"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_alert_symbol"), table_name="alert")
    op.drop_index(op.f("ix_alert_user_id"), table_name="alert")
    op.drop_table("alert")
    op.drop_index(op.f("ix_watchlist_item_symbol"), table_name="watchlist_item")
    op.drop_index(op.f("ix_watchlist_item_user_id"), table_name="watchlist_item")
    op.drop_table("watchlist_item")
