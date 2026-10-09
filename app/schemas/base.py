"""Base response schemas shared across all endpoints."""

from uuid import UUID, uuid4

from asgi_correlation_id import correlation_id
from pydantic import BaseModel, Field

from app.models.base import UTCDateTimeMixin


def _get_request_id() -> UUID:
    """Return the current request's correlation ID, or a fresh UUID as fallback."""
    value = correlation_id.get()
    return UUID(value) if value else uuid4()


class BaseResponse(UTCDateTimeMixin, BaseModel):
    """Base response model that all endpoint responses inherit from.

    request_id is auto-populated from the CorrelationIdMiddleware ContextVar —
    no endpoint needs to pass it explicitly.

    Inherits UTCDateTimeMixin so any datetime field added by a subclass is
    serialised with an explicit UTC offset (see app/models/base.py for why).
    """

    request_id: UUID = Field(default_factory=_get_request_id, description="Unique identifier for this request")
