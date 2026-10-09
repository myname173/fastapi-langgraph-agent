"""Base models and common imports for all models."""

from datetime import UTC, datetime
from typing import Optional

from pydantic import BaseModel as PydanticBaseModel
from pydantic import field_serializer
from sqlmodel import Field, SQLModel


def utc_isoformat(value: Optional[datetime]) -> Optional[str]:
    """Render a datetime as ISO 8601 with an explicit UTC offset.

    Args:
        value: The datetime to serialise, or ``None``.

    Returns:
        An ISO 8601 string such as ``2026-10-06T09:32:38.940196Z``, or
        ``None`` when ``value`` is ``None``.

    Note:
        A naive value is assumed to already be UTC. Every timestamp column in
        this schema is ``TIMESTAMP WITHOUT TIME ZONE`` and the application only
        ever writes ``datetime.now(UTC)``, so the database round-trip strips
        the tzinfo without changing the instant.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class UTCDateTimeMixin(PydanticBaseModel):
    """Serialise every datetime field with an explicit UTC offset.

    Why this exists: all timestamp columns are ``TIMESTAMP WITHOUT TIME ZONE``,
    so a datetime written as ``datetime.now(UTC)`` comes back from the database
    **naive**. Pydantic then rendered it as ``2026-10-06T09:32:38.940196`` with
    no offset. Browsers parse a naive ISO date-time as *local* time (per the
    ECMAScript spec), so every timestamp shown in the UI was shifted by the
    viewer's UTC offset — 8 hours for GMT+8. The task list showed a
    two-minute-old task as "8 小时前".

    Emitting the offset makes the wire format unambiguous; the browser's own
    ``new Date()`` / ``toLocaleString()`` then renders it in local time.

    ``check_fields=False`` lets a single mixin cover every model — each class
    only serialises the datetime fields it actually declares.
    """

    @field_serializer(
        "created_at",
        "updated_at",
        "completed_at",
        "fired_at",
        "timestamp",
        "expires_at",
        check_fields=False,
    )
    def _serialize_utc_datetime(self, value: Optional[datetime]) -> Optional[str]:
        """Render a datetime field with an explicit UTC offset."""
        return utc_isoformat(value)


class BaseModel(UTCDateTimeMixin, SQLModel):
    """Base model with common fields."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
