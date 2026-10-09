"""Pydantic schemas for Goal API."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.base import UTCDateTimeMixin
from app.models.goal import GoalStatus


class GoalCreate(BaseModel):
    """Request body for creating a goal."""

    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class GoalUpdate(BaseModel):
    """Request body for updating a goal. All fields optional."""

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    status: Optional[GoalStatus] = None


class GoalResponse(UTCDateTimeMixin, BaseModel):
    """Goal response without task details."""

    id: str
    user_id: int
    title: str
    description: str
    status: GoalStatus
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        """Enable ORM mode for SQLModel compatibility."""

        from_attributes = True


class GoalDetailResponse(GoalResponse):
    """Goal response with associated tasks and progress stats."""

    tasks: List[dict] = []
    progress: dict = {}


class TaskAssociateRequest(BaseModel):
    """Request body for associating a task with a goal."""

    task_id: str = Field(..., min_length=1)
