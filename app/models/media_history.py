"""MediaHistory model for audit trail of media generations."""

from datetime import datetime, UTC
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class MediaType(str, Enum):
    """Media generation type."""

    IMAGE = "image"
    VIDEO = "video"


class MediaStatus(str, Enum):
    """Media generation outcome."""

    SUCCESS = "success"
    FAILED = "failed"


class MediaHistory(SQLModel, table=True):
    """MediaHistory model for storing media generation audit records.

    Each row records one generation request (image or video) made via the
    /api/v1/media endpoints, regardless of success or failure. This gives
    users visibility into their generation history and supports quota
    auditing (L-14 fix).

    Attributes:
        id: UUID primary key.
        user_id: User who triggered the generation (indexed for per-user queries).
        media_type: "image" or "video".
        prompt: The text prompt submitted.
        model: DashScope model used (e.g. wan2.6-t2i).
        status: "success" or "failed".
        result_url: Primary result URL (first image URL, or video_url). May be
            empty for failed generations. Note: DashScope URLs expire in 24-48h.
        image_count: Number of images returned (0 for video or failures).
        task_id: DashScope async task ID (video only).
        size: Image size string (e.g. "1024*1024"), image only.
        resolution: Video resolution ("720P"/"1080P"), video only.
        duration: Video length in seconds, video only.
        error_message: Error details when status=failed.
        created_at: When the generation completed.
    """

    id: str = Field(primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    media_type: MediaType = Field(default=MediaType.IMAGE)
    prompt: str = Field(default="")
    model: str = Field(default="")
    status: MediaStatus = Field(default=MediaStatus.SUCCESS)
    result_url: Optional[str] = Field(default=None)
    image_count: int = Field(default=0)
    task_id: Optional[str] = Field(default=None)
    size: Optional[str] = Field(default=None)
    resolution: Optional[str] = Field(default=None)
    duration: Optional[int] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
