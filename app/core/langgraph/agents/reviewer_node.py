"""Reviewer node — reviews code changes and test results for quality.

Extracted from the former ``SupervisorGraph._reviewer_node`` method.
"""

from langgraph.errors import GraphInterrupt
from langgraph.graph.state import Command

from app.core.langgraph.agents.context import (
    AgentContext,
    build_error_command,
    call_llm_structured,
)
from app.core.langgraph.agents.supervisor_node import NodeCallable
from app.core.logging import logger
from app.core.metrics import sdlc_human_interventions_total
from app.core.prompts import load_agent_prompt
from app.schemas import ReviewerOutput, SDLCState
from app.utils import prepare_sdlc_messages


def make_reviewer_node(ctx: AgentContext) -> NodeCallable:
    """Create the reviewer node function."""

    async def reviewer_node(state: SDLCState) -> Command:
        system_prompt = load_agent_prompt(
            "reviewer",
            requirements=state.get("requirements", ""),
            plan=state.get("plan", []),
            code_changes=state.get("code_changes", []),
            test_results=state.get("test_results", []),
            image_contexts=state.get("image_contexts", []),
        )
        messages = prepare_sdlc_messages(state["messages"], system_prompt)

        try:
            response = await call_llm_structured(ctx, "reviewer", "reviewing", messages, ReviewerOutput)

            feedback_data = [item.model_dump() for item in response.feedback] if response.feedback else []
            # BUG-4 fix: persist security_issues into state (previously
            # discarded, making the field dead code across the whole chain).
            security_issues_data = (
                [issue.model_dump() for issue in response.security_issues] if response.security_issues else []
            )

            needs_approval = any(item["severity"] in ["high", "critical"] for item in feedback_data) or bool(
                security_issues_data
            )

            if needs_approval:
                sdlc_human_interventions_total.labels(agent_name="reviewer").inc()

            logger.info(
                "sdlc_reviewer_completed",
                feedback_count=len(feedback_data),
                needs_approval=needs_approval,
            )

            update = {
                "messages": [{"role": "assistant", "content": response.summary}],
                "feedback": feedback_data,
                "security_issues": security_issues_data,
                "status": "reviewing",
                "human_approval_needed": needs_approval,
            }

            # C-7 fix: do NOT call langgraph_interrupt() here. Previously
            # interrupt() raised before this Command returned, so the update
            # (feedback, security_issues, human_approval_needed=True) was
            # never committed to the checkpoint — the approver had nothing
            # to act on and stream_events saw GraphInterrupt as an error.
            # Now reviewer always returns; supervisor_node checks
            # human_approval_needed and interrupts there (after the update
            # is persisted).
            return Command(update=update, goto="supervisor")
        except GraphInterrupt:
            raise
        except Exception as e:
            return build_error_command("reviewer", "reviewing", "reviewing", e)

    return reviewer_node
