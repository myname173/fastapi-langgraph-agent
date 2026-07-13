"""SDLC agent node factories.

Each module exports a ``make_<name>_node(ctx)`` factory that returns a
LangGraph-compatible node callable ``(state, config) -> Command``.  The
factory captures the :class:`AgentContext` via closure, keeping the node
signature transparent to LangGraph's ``add_node``.

Node modules
------------

- :mod:`supervisor_node` — top-level coordinator / router
- :mod:`planner_node` — generates structured task plan
- :mod:`coder_node` — writes/edits code with file tools (largest node)
- :mod:`tester_node` — executes tests and reports results
- :mod:`reviewer_node` — reviews changes for quality and approval
"""

from app.core.langgraph.agents.context import AgentContext, build_default_context
from app.core.langgraph.agents.coder_node import make_coder_node
from app.core.langgraph.agents.planner_node import make_planner_node
from app.core.langgraph.agents.reviewer_node import make_reviewer_node
from app.core.langgraph.agents.supervisor_node import NodeCallable, make_supervisor_node
from app.core.langgraph.agents.tester_node import make_tester_node

__all__ = [
    "AgentContext",
    "NodeCallable",
    "build_default_context",
    "make_coder_node",
    "make_planner_node",
    "make_reviewer_node",
    "make_supervisor_node",
    "make_tester_node",
]
