"""Risk-profile model for the chat stock-advisor.

A single row per user capturing risk tolerance, goals, horizon and experience.
Authoritative store for the advisor's personalisation; surfaced into the chat
system prompt so the advisor tailors its guidance to the user.
"""

from __future__ import annotations

from datetime import datetime, UTC

from sqlmodel import Field, SQLModel

from app.models.portfolio import RiskTolerance


class RiskProfile(SQLModel, table=True):
    """A user's investment risk profile (one row per user)."""

    __tablename__ = "risk_profile"  # pyright: ignore[reportAssignmentType]

    user_id: int = Field(primary_key=True)
    risk_tolerance: RiskTolerance = Field(default=RiskTolerance.BALANCED)
    investment_goals: str = Field(default="")  # 投资目标描述
    horizon: str = Field(default="")  # 投资周期，如 "3年以上"
    monthly_invest: float = Field(default=0.0)  # 月均可投资金额（元）
    experience: str = Field(default="")  # 投资经验描述
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
