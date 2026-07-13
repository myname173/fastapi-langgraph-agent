"""Media generation API endpoints.

Provides text-to-image and text-to-video generation via DashScope's
native API. Decoupled from the SDLC orchestrator — these endpoints
can be used standalone or integrated into agent workflows later.
"""

import re
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.api.v1.auth import get_current_session
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.media_history import MediaStatus, MediaType
from app.models.session import Session
from app.services.media_generation_service import media_service
from app.services.media_history_service import media_history_service
from app.services.quota_service import quota_service
from app.utils.errors import safe_500

router = APIRouter()

# L-9 fix: DashScope wan2.6-t2i expects sizes as "WIDTH*HEIGHT" (e.g.
# "1024*1024"). Known supported sizes: 1024*1024, 720*1280, 1280*720.
# We validate the format with a regex rather than a hard-coded allowlist
# so new sizes DashScope may add do not break the endpoint.
_IMAGE_SIZE_PATTERN = re.compile(r"^\d+\*\d+$")


class ImageGenerationRequest(BaseModel):
    """Request body for text-to-image generation."""

    prompt: str = Field(..., min_length=1, max_length=2100, description="Image description")
    size: str = Field(default="1024*1024", description='Image size, e.g. "1024*1024"')
    n: int = Field(default=1, ge=1, le=4, description="Number of images (1-4)")

    @field_validator("size")
    @classmethod
    def validate_size_format(cls, v: str) -> str:
        """Validate that size matches the DashScope ``WIDTH*HEIGHT`` format.

        Catches malformed inputs (empty string, wrong separator, non-numeric
        dimensions) at the API boundary so they never reach DashScope and
        waste a billable API call (L-9 fix).
        """
        if not _IMAGE_SIZE_PATTERN.match(v):
            raise ValueError(f'size must be in "WIDTH*HEIGHT" format (e.g. "1024*1024"), got: {v!r}')
        width_str, height_str = v.split("*")
        width, height = int(width_str), int(height_str)
        if width <= 0 or height <= 0:
            raise ValueError(f"size dimensions must be positive, got: {v!r}")
        return v


class VideoGenerationRequest(BaseModel):
    """Request body for text-to-video generation."""

    prompt: str = Field(..., min_length=1, max_length=2000, description="Video description")
    duration: int = Field(default=5, ge=2, le=15, description="Video length in seconds (2-15)")
    # L-9 fix: constrain resolution to the two values DashScope wan2.7-t2v
    # actually accepts — any other string wastes a billable API call.
    resolution: Literal["720P", "1080P"] = Field(default="720P", description='"720P" or "1080P"')


class MediaHistoryItem(BaseModel):
    """A single media generation history entry (L-14)."""

    id: str
    media_type: str
    prompt: str
    model: str
    status: str
    result_url: Optional[str] = None
    image_count: int = 0
    task_id: Optional[str] = None
    size: Optional[str] = None
    resolution: Optional[str] = None
    duration: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime


@router.post("/generate-image")
@limiter.limit("10 per minute")
async def generate_image(
    request: Request,
    body: ImageGenerationRequest,
    session: Session = Depends(get_current_session),
):
    """Generate image(s) from a text prompt via Bailian wan2.6-t2i.

    Returns a list of image URLs. Free quota: ~500 images (90 days).
    """
    # M-13 fix: enforce per-user daily media quota before hitting DashScope.
    if not quota_service.check_media_quota(session.user_id):
        raise HTTPException(
            status_code=429,
            detail="Daily media generation quota exceeded. Please try again tomorrow.",
        )
    try:
        result = await media_service.generate_image(body.prompt, body.size, body.n)
        # M-13 fix: only count successful generations toward the quota.
        quota_service.increment_media_usage(session.user_id)
        # L-14 fix: record successful generation in history (best-effort).
        await media_history_service.record_generation(
            user_id=session.user_id,
            media_type=MediaType.IMAGE,
            prompt=body.prompt,
            model=result.get("model", ""),
            status=MediaStatus.SUCCESS,
            result_url=result["images"][0] if result["images"] else None,
            image_count=len(result["images"]),
            size=body.size,
        )
        logger.info(
            "image_generation_request",
            session_id=session.id,
            user_id=session.user_id,
            image_count=len(result["images"]),
        )
        return result
    except Exception as e:
        # L-14 fix: record failed generation so users can see what went wrong.
        await media_history_service.record_generation(
            user_id=session.user_id,
            media_type=MediaType.IMAGE,
            prompt=body.prompt,
            model=settings.IMAGE_GENERATION_MODEL,
            status=MediaStatus.FAILED,
            size=body.size,
            error_message=str(e)[:1000],
        )
        raise safe_500(e, context="image_generation")


@router.post("/generate-video")
@limiter.limit("5 per minute")
async def generate_video(
    request: Request,
    body: VideoGenerationRequest,
    session: Session = Depends(get_current_session),
):
    """Generate a video from a text prompt via Bailian wan2.7-t2v.

    This is an async operation: submit + poll. The endpoint blocks until
    the video is ready (typically 1-5 minutes). Free quota: 50 seconds total.
    """
    # M-13 fix: enforce per-user daily media quota before hitting DashScope.
    if not quota_service.check_media_quota(session.user_id):
        raise HTTPException(
            status_code=429,
            detail="Daily media generation quota exceeded. Please try again tomorrow.",
        )
    # Track task_id across the try block so the failure path can record it
    # (M-11 fix surfaces task_id in the TimeoutError message).
    video_task_id: Optional[str] = None
    try:
        # M-12 fix: limit concurrent video generations per user. Video
        # polling blocks 1-5 min; the per-user semaphore (default 1)
        # serializes them so one user can't tie up event-loop capacity.
        async with quota_service.acquire_video_slot(session.user_id):
            result = await media_service.generate_video(body.prompt, body.duration, body.resolution)
        video_task_id = result.get("task_id")
        # M-13 fix: only count successful generations toward the quota.
        quota_service.increment_media_usage(session.user_id)
        # L-14 fix: record successful generation in history (best-effort).
        await media_history_service.record_generation(
            user_id=session.user_id,
            media_type=MediaType.VIDEO,
            prompt=body.prompt,
            model=result.get("model", ""),
            status=MediaStatus.SUCCESS,
            result_url=result.get("video_url"),
            task_id=video_task_id,
            resolution=body.resolution,
            duration=body.duration,
        )
        logger.info(
            "video_generation_request",
            session_id=session.id,
            user_id=session.user_id,
            task_id=video_task_id,
        )
        return result
    except Exception as e:
        # L-14 fix: record failed generation. Preserve task_id if the
        # error happened after submit (e.g. polling timeout) so the user
        # can retry polling the same task later.
        await media_history_service.record_generation(
            user_id=session.user_id,
            media_type=MediaType.VIDEO,
            prompt=body.prompt,
            model=settings.VIDEO_GENERATION_MODEL,
            status=MediaStatus.FAILED,
            task_id=video_task_id,
            resolution=body.resolution,
            duration=body.duration,
            error_message=str(e)[:1000],
        )
        raise safe_500(e, context="video_generation")


@router.get("/history")
@limiter.limit("30 per minute")
async def list_media_history(
    request: Request,
    session: Session = Depends(get_current_session),
    limit: int = Query(default=50, ge=1, le=200, description="Max results"),
) -> List[MediaHistoryItem]:
    """List the current user's media generation history, newest first (L-14).

    DashScope image URLs expire in 24-48h; historical URLs may no longer
    be accessible. Use the ``status`` field to distinguish successes from
    failures.
    """
    records = await media_history_service.get_history_by_user(session.user_id, limit=limit)
    return [
        MediaHistoryItem(
            id=r.id,
            media_type=r.media_type.value if hasattr(r.media_type, "value") else str(r.media_type),
            prompt=r.prompt,
            model=r.model,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            result_url=r.result_url,
            image_count=r.image_count,
            task_id=r.task_id,
            size=r.size,
            resolution=r.resolution,
            duration=r.duration,
            error_message=r.error_message,
            created_at=r.created_at,
        )
        for r in records
    ]
