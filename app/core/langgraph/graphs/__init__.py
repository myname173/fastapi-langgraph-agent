"""LangGraph graph configurations for SDLC Orchestrator.

Backward-compatibility shim: ``SupervisorGraph`` now lives in
:mod:`app.core.langgraph.workflows.supervisor`.  This module re-exports it
so existing imports (``from app.core.langgraph.graphs import SupervisorGraph``)
continue to work during the migration.
"""

from app.core.langgraph.workflows.supervisor import SupervisorGraph

__all__ = ["SupervisorGraph"]
