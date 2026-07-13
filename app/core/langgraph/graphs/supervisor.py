"""Deprecated — use :mod:`app.core.langgraph.workflows.supervisor` instead.

This module is kept as a backward-compatibility re-export.  The
``SupervisorGraph`` class now lives in
:mod:`app.core.langgraph.workflows.supervisor`.
"""

import warnings

from app.core.langgraph.workflows.supervisor import SupervisorGraph

warnings.warn(
    "app.core.langgraph.graphs.supervisor is deprecated; import from app.core.langgraph.workflows instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["SupervisorGraph"]
