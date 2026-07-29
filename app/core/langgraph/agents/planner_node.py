"""Planner node — generates a structured task plan from requirements.

Extracted from the former ``SupervisorGraph._planner_node`` method.
"""

from langgraph.graph.state import Command

from app.core.langgraph.agents.context import (
    AgentContext,
    build_error_command,
    call_llm_structured,
)
from app.core.langgraph.agents.supervisor_node import NodeCallable
from app.core.logging import logger
from app.core.prompts import load_agent_prompt
from app.schemas import PlannerOutput, SDLCState
from app.utils import prepare_sdlc_messages


def make_planner_node(ctx: AgentContext) -> NodeCallable:
    """Create the planner node function."""

    async def planner_node(state: SDLCState) -> Command:
        system_prompt = load_agent_prompt(
            "planner",
            requirements=state.get("requirements", ""),
            long_term_memory=state.get("long_term_memory", ""),
            product_spec=state.get("product_spec", []),
            image_contexts=state.get("image_contexts", []),
        )
        messages = prepare_sdlc_messages(state["messages"], system_prompt)

        try:
            response = await call_llm_structured(ctx, "planner", "planning", messages, PlannerOutput)

            plan_data = [task.model_dump() for task in response.plan] if response.plan else []
            logger.info(
                "sdlc_planner_completed",
                plan_task_count=len(plan_data),
                feasibility=response.feasibility,
            )

            # Technical feasibility gate: if planner determines the task is
            # technically blocked, pause for human approval before coding.
            # Reuses the same human_approval_needed mechanism as PO/reviewer.
            update_dict: dict = {
                "messages": [{"role": "assistant", "content": response.summary}],
                "plan": plan_data,
                "status": "planning",
            }

            if response.feasibility == "blocked":
                update_dict["human_approval_needed"] = True
                logger.info(
                    "planner_feasibility_blocked",
                    rationale=response.feasibility_rationale[:200],
                )

            return Command(
                update=update_dict,
                goto="supervisor",
            )
        except Exception as e:
            return build_error_command("planner", "planning", "planning", e)

    return planner_node
