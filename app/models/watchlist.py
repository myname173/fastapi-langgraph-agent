"""Watchlist model for the chat stock-advisor (自选股).

A lightweight, user-curated list of ticker symbols the user wants to keep an
eye on. Mirrors the field layout of ``portfolio.py`` (explicit ``__tablename__``
so Alembic-created relations match SQLModel's expectations).
"""

from __future__ import annotations

from datetime import datetime, UTC

from sqlmodel import Field, SQLModel

from app.models.base import UTCDateTimeMixin


class WatchlistItem(UTCDateTimeMixin, SQLModel, table=True):
    """A single symbol in a user's watchlist."""

    __tablename__ = "watchlist_item"  # pyright: ignore[reportAssignmentType]

    id: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    symbol: str = Field(index=True)  # e.g. sh600519
    name: str = Field(default="")  # 股票名称
    notes: str = Field(default="")  # 用户备注
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
