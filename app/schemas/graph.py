"""This file contains the graph schema for the application."""

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages
from operator import add

from app.utils.graph import code_changes_reducer, replace_list_reducer


class GraphState(TypedDict):
    """State definition for the LangGraph Agent/Workflow."""

    messages: Annotated[list, add_messages]
    long_term_memory: str
    vision_context: str


class SDLCState(TypedDict):
    """State definition for the SDLC Orchestrator multi-agent workflow.

    All fields must be JSON-serializable for AsyncPostgresSaver checkpointing.
    Complex structures are stored as dicts, with Pydantic models used for API validation.
    """

    messages: Annotated[list, add_messages]
    long_term_memory: str

    requirements: str
    project_context: str
    user_id: str

    plan: Annotated[list[dict], replace_list_reducer]
    code_changes: Annotated[list[dict], code_changes_reducer]
    test_results: Annotated[list[dict], add]
    feedback: Annotated[list[dict], add]
    security_issues: Annotated[list[dict], add]

    errors: Annotated[list[dict], add]
    artifacts: Annotated[list[dict], add]

    image_contexts: Annotated[list[dict], add]

    human_approval_needed: bool
    status: Literal["planning", "coding", "testing", "reviewing", "completed"]
    is_complete: bool

    validation_results: Annotated[list[dict], replace_list_reducer]
    coder_retry_count: int
    reviewer_retry_count: int
