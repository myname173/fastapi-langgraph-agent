"""SDLCTask model for SDLC workflow management."""

from datetime import datetime, UTC
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class SDLCTaskStatus(str, Enum):
    """SDLC task status enumeration."""

    PLANNING = "planning"
    CODING = "coding"
    TESTING = "testing"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class SDLCTask(SQLModel, table=True):
    """SDLCTask model for storing SDLC workflow tasks.

    Attributes:
        id: The primary key (UUID)
        thread_id: Associated thread ID for checkpoint lookup
        user_id: User ID who created the task
        status: Current task status
        requirements: Original user requirements
        plan: JSON serialized task plan
        created_at: When the task was created
        updated_at: When the task was last updated
        completed_at: When the task was completed (optional)
    """

    # NOTE: explicit __tablename__ is REQUIRED because the alembic migration
    # ``a3f7c92e1b4d_add_sdlc_task_and_approval_log.py`` created the table
    # with the name ``sdlc_task`` (with underscore). Without this, SQLModel
    # derives ``sdlctask`` from the class name and queries against a
    # non-existent relation, returning HTTP 500.
    __tablename__ = "sdlc_task"

    id: str = Field(primary_key=True)
    thread_id: str = Field(index=True)
    user_id: Optional[int] = Field(default=None)
    status: SDLCTaskStatus = Field(default=SDLCTaskStatus.PLANNING)
    requirements: str = Field(default="")
    plan: str = Field(default="[]")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: Optional[datetime] = Field(default=None)
