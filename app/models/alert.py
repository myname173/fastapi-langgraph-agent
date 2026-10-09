"""Price alert model for the chat stock-advisor.

A user-configured price-based trigger. When the current price crosses the
configured threshold in the specified direction, the alert fires (once).
One-shot semantics: after triggering, ``active`` flips to false.
"""

from __future__ import annotations

from datetime import datetime, UTC
from enum import Enum

from sqlmodel import Field, SQLModel

from app.models.base import UTCDateTimeMixin


class AlertCondition(str, Enum):
    """Direction of the price-cross trigger."""

    ABOVE = "above"  # fire when price rises above threshold
    BELOW = "below"  # fire when price falls below threshold


class Alert(UTCDateTimeMixin, SQLModel, table=True):
    """A user-defined price-breakout alert."""

    __tablename__ = "alert"  # pyright: ignore[reportAssignmentType]

    id: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    symbol: str = Field(index=True)  # e.g. sh600519
    name: str = Field(default="")  # 股票名称
    condition: AlertCondition = Field(default=AlertCondition.ABOVE)
    price: float = Field(default=0.0)  # trigger price (元)
    active: bool = Field(default=True)  # False after the alert fires
    fired_at: datetime | None = Field(default=None, nullable=True)
    note: str = Field(default="")  # 用户备注，如 "突破前高1200"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
