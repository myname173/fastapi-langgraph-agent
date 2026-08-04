"""Context propagated from a user task to individual LLM attempts."""

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LLMExecutionContext:
    """Non-content identifiers used for policy enforcement and audit."""

    execution_id: Optional[str] = None
    user_id: Optional[int] = None
    agent_name: str = "unknown"
    purpose: str = "general"


_context: contextvars.ContextVar[LLMExecutionContext | None] = contextvars.ContextVar(
    "llm_execution_context", default=None
)


def get_llm_execution_context() -> LLMExecutionContext:
    """Return the current task-scoped execution context."""
    return _context.get() or LLMExecutionContext()


@contextlib.contextmanager
def set_llm_execution_context(
    *,
    execution_id: Optional[str],
    user_id: Optional[int],
    agent_name: str,
    purpose: str,
):
    """Temporarily bind an execution context across awaited calls."""
    token = _context.set(LLMExecutionContext(execution_id, user_id, agent_name, purpose))
    try:
        yield
    finally:
        _context.reset(token)
