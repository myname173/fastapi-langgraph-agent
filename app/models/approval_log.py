"""ApprovalLog model for audit trail of human approvals."""

from datetime import datetime, UTC
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel

from app.models.base import UTCDateTimeMixin


class ApprovalAction(str, Enum):
    """Approval action types."""

    APPROVE = "approve"
    REJECT = "reject"


class ApprovalStep(str, Enum):
    """Approval step types."""

    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"
    PLANNING = "planning"
    WORKFLOW = "workflow"


class ApprovalLog(UTCDateTimeMixin, SQLModel, table=True):
    """ApprovalLog model for storing human approval audit records.

    Attributes:
        id: The primary key (UUID)
        task_id: Associated task ID
        step: The step being approved/rejected
        action: approve or reject
        user_id: User ID who performed the action
        reason: Optional reason for the action
        timestamp: When the action was performed
        state_hash: Hash of the state for verification
        status_before: Status before the action
        status_after: Status after the action
    """

    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    step: ApprovalStep = Field(default=ApprovalStep.REVIEWER)
    action: ApprovalAction = Field(default=ApprovalAction.APPROVE)
    user_id: Optional[int] = Field(default=None)
    reason: Optional[str] = Field(default=None)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state_hash: Optional[str] = Field(default=None)
    status_before: Optional[str] = Field(default=None)
    status_after: Optional[str] = Field(default=None)
