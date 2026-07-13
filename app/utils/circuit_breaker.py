"""Circuit breaker for unreliable external dependencies (pgvector, etc.).

Prevents cascading failures when a dependency is persistently down: after
``failure_threshold`` consecutive failures the circuit "opens" and
short-circuits calls for ``cooldown_seconds``. After the cooldown elapses,
one "probe" call is allowed (half-open); success closes the circuit,
failure re-opens it.

The breaker is intentionally lightweight and dependency-free (stdlib +
structlog only) so it can be reused by any async service that talks to a
flaky backend — currently ``memory_service``, ``code_index_service`` and
``knowledge_base_service`` (all three wrap mem0 / pgvector).

Thread-safety: state mutations are guarded by a ``threading.Lock`` because
the breaker may be touched from both the event loop and ``run_in_thread``
worker threads.
"""

import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from app.core.logging import logger

T = TypeVar("T")


class CircuitState(Enum):
    """States of a :class:`CircuitBreaker`."""

    CLOSED = "closed"  # normal operation — all calls go through
    OPEN = "open"  # short-circuiting — failures exceeded threshold
    HALF_OPEN = "half_open"  # cooldown elapsed — one probe call allowed


class CircuitBreaker:
    """Simple async circuit breaker with consecutive-failure counting.

    Usage as an async context manager (preferred for try/except services)::

        async with breaker.guard() as proceed:
            if not proceed:
                return ""          # short-circuited — backend is down
            result = await flaky_call()
            return result

    Or as a higher-order call for simple pass/fail semantics::

        result = await breaker.call(flaky_async_fn, *args)

    Args:
        name: Human-readable identifier used in log events.
        failure_threshold: Consecutive failures required to open the
            circuit. Defaults to ``5``.
        cooldown_seconds: Seconds the circuit stays open before allowing
            a half-open probe. Defaults to ``60.0``.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Current state, auto-transitioning OPEN -> HALF_OPEN after cooldown."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self._cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    @property
    def is_open(self) -> bool:
        """True when the circuit is open (calls should be short-circuited)."""
        return self.state == CircuitState.OPEN

    def _allow_call(self) -> bool:
        """Check whether a call may proceed, transitioning OPEN -> HALF_OPEN."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self._cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    return True  # allow one probe
                return False  # still cooling down
            return True  # CLOSED or HALF_OPEN

    def record_success(self) -> None:
        """Mark a call as successful — resets the failure count and closes."""
        with self._lock:
            if self._state != CircuitState.CLOSED:
                logger.info(
                    "circuit_breaker_closed",
                    breaker=self.name,
                    previous_state=self._state.value,
                )
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Mark a call as failed — may open the circuit if threshold reached."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — re-open immediately.
                self._state = CircuitState.OPEN
                logger.warning(
                    "circuit_breaker_reopened",
                    breaker=self.name,
                    reason="half_open_probe_failed",
                )
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "circuit_breaker_opened",
                    breaker=self.name,
                    failure_count=self._failure_count,
                    threshold=self._failure_threshold,
                    cooldown_seconds=self._cooldown_seconds,
                )

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[bool]:
        """Async context manager that gates a single backend call.

        Yields ``True`` when the call should proceed (circuit closed or
        half-open probe). Yields ``False`` when the call should be
        short-circuited (circuit open). On exit:

        - If the body raised → :meth:`record_failure` is called and the
          exception propagates to the caller (which typically catches it
          and returns a safe default).
        - If the body returned cleanly → :meth:`record_success` is called.
        - If short-circuited (yielded ``False``) → nothing is recorded.

        This makes it safe to compose with the existing try/except pattern
        used by the memory/code-index/knowledge-base services::

            try:
                async with breaker.guard() as proceed:
                    if not proceed:
                        return ""
                    return await backend.search(...)
            except Exception as e:
                # failure already recorded by the guard's __aexit__
                logger.error("search_failed", error=str(e))
                return ""
        """
        if not self._allow_call():
            # Short-circuit: yield False and bail without recording.
            yield False
            return
        # Normal path: yield True, then record success/failure on exit.
        try:
            yield True
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args: object,
        **kwargs: object,
    ) -> T:
        """Call an async ``func`` through the breaker.

        Returns ``func``'s result on success. Raises :class:`CircuitOpenError`
        when short-circuited, or the original exception from ``func`` on
        failure (the failure is recorded before re-raising).
        """
        if not self._allow_call():
            raise CircuitOpenError(self.name)
        try:
            result = await func(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


class CircuitOpenError(Exception):
    """Raised by :meth:`CircuitBreaker.call` when the circuit is open."""

    def __init__(self, breaker_name: str) -> None:
        self.breaker_name = breaker_name
        super().__init__(f"Circuit breaker '{breaker_name}' is open")
