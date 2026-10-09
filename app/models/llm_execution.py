"""Persistent, content-free audit records for LLM execution attempts."""

from datetime import UTC, datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.models.base import UTCDateTimeMixin


class LLMExecution(UTCDateTimeMixin, SQLModel, table=True):
    """One attempted model invocation within a chat or SDLC execution."""

    __tablename__ = "llm_execution"  # pyright: ignore[reportAssignmentType]

    id: str = Field(primary_key=True)
    execution_id: str = Field(index=True)
    user_id: Optional[int] = Field(default=None, index=True)
    agent_name: str = Field(default="unknown")
    purpose: str = Field(default="general")
    requested_model: str
    effective_model: str
    actual_model: str
    deployment: str
    attempt: int
    outcome: str
    duration_ms: int
    error_type: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
