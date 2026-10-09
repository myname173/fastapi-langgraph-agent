"""Portfolio models for the chat stock-advisor portfolio management.

Two tables: ``Portfolio`` (a user's watch/hold book) and ``Holding`` (a single
position within it). Both use explicit ``__tablename__`` so Alembic-created
relations match SQLModel's expectations (mirrors ``sdlc_task.py``).
"""

from __future__ import annotations

from datetime import datetime, UTC
from enum import Enum

from sqlmodel import Field, SQLModel

from app.models.base import UTCDateTimeMixin


class RiskTolerance(str, Enum):
    """User risk tolerance for portfolio-level guidance."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class Portfolio(UTCDateTimeMixin, SQLModel, table=True):
    """A user's investment portfolio."""

    __tablename__ = "portfolio"  # pyright: ignore[reportAssignmentType]

    id: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    name: str = Field(default="我的组合")
    risk_tolerance: RiskTolerance = Field(default=RiskTolerance.BALANCED)
    cash: float = Field(default=0.0)  # 可用现金（元）
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Holding(UTCDateTimeMixin, SQLModel, table=True):
    """A single position inside a portfolio."""

    __tablename__ = "holding"  # pyright: ignore[reportAssignmentType]

    id: str = Field(primary_key=True)
    portfolio_id: str = Field(index=True, foreign_key="portfolio.id")
    symbol: str = Field(index=True)  # e.g. sh600519
    name: str = Field(default="")
    shares: float = Field(default=0.0)  # 持仓股数
    cost_price: float = Field(default=0.0)  # 成本价（元/股）
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
