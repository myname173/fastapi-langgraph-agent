"""Goal model for cross-session long-term objective tracking."""

from datetime import datetime, UTC
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class GoalStatus(str, Enum):
    """Goal status enumeration."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class Goal(SQLModel, table=True):
    """A long-term objective that groups multiple SDLC tasks.

    Attributes:
        id: UUID primary key.
        user_id: Owner user ID.
        title: Short human-readable goal title.
        description: Detailed goal description (markdown allowed).
        status: ACTIVE / COMPLETED / ARCHIVED.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
        completed_at: Set when status transitions to COMPLETED.
    """

    id: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    title: str = Field(default="")
    description: str = Field(default="")
    status: GoalStatus = Field(default=GoalStatus.ACTIVE)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: Optional[datetime] = Field(default=None)
