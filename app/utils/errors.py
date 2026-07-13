"""Sanitized error responses for HTTP handlers.

Replaces the unsafe pattern of ``raise HTTPException(500, str(e))`` which
leaks internals to the client — ``str(e)`` routinely contains database
connection strings, absolute filesystem paths, model API error bodies,
and SQL fragments. Those details belong in server logs (where they can be
correlated by ``request_id``), never in the response body.

In development/test we keep ``str(e)`` so debugging is fast; in staging
and production the client only receives a generic message plus the
``request_id`` for support correlation.
"""

from typing import Optional

from asgi_correlation_id import correlation_id
from fastapi import HTTPException, status

from app.core.config import Environment, settings
from app.core.logging import logger


def _correlation_id() -> Optional[str]:
    """Return the current request's correlation id, if any.

    Returns:
        The id string, or ``None`` when called outside a request context
        (e.g. background tasks).
    """
    try:
        return correlation_id.get()
    except LookupError:
        return None


def safe_500(error: Exception, *, context: str) -> HTTPException:
    """Build an HTTP 500 exception that never leaks internal details.

    Always logs the full exception server-side via ``logger.exception``
    (preserves the traceback) with the error type, context, and request
    id, so engineers can find the root cause. In production/staging the
    returned detail is a generic message; only in development/test is
    the raw ``str(e)`` exposed for fast iteration.

    Args:
        error: The exception that triggered the failure.
        context: Short label identifying the call site (e.g. ``"chat"``,
            ``"sdlc_orchestrate"``).

    Returns:
        An ``HTTPException`` with status 500 and a sanitized detail.
    """
    request_id = _correlation_id()
    logger.exception(
        "internal_error",
        context=context,
        error_type=type(error).__name__,
        request_id=request_id,
    )

    if settings.ENVIRONMENT in (Environment.DEVELOPMENT, Environment.TEST):
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error))

    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"detail": "Internal server error", "request_id": request_id},
    )


def safe_stream_error(error: Exception, *, context: str) -> str:
    """Build a sanitized error message for SSE/streaming responses.

    Streaming endpoints cannot ``raise safe_500(...)`` inside a generator
    (the exception would not reach the client as an HTTP response), so
    they previously yielded ``str(e)`` directly — leaking database
    connection strings, filesystem paths, and internal stack details to
    the client. This helper mirrors :func:`safe_500`'s sanitization
    policy but returns a plain string suitable for inclusion in an SSE
    ``data:`` payload.

    Always logs the full exception server-side via ``logger.exception``
    (preserves the traceback) with the error type, context, and request
    id. In production/staging only a generic message + request_id is
    returned; in development/test the raw ``str(error)`` is exposed for
    fast iteration.

    Args:
        error: The exception that triggered the failure.
        context: Short label identifying the call site (e.g.
            ``"sdlc_stream"``, ``"chat_stream"``).

    Returns:
        A sanitized message string safe to send to the client.
    """
    request_id = _correlation_id()
    logger.exception(
        "internal_error_stream",
        context=context,
        error_type=type(error).__name__,
        request_id=request_id,
    )

    if settings.ENVIRONMENT in (Environment.DEVELOPMENT, Environment.TEST):
        return str(error)

    return f"Internal server error (request_id={request_id})"
