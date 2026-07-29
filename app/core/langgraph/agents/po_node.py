"""PO (Product Owner) node — produces a structured ProductSpec before planning.

Implements the spec-driven development pattern: the PO challenges the
requirement's WHY, defines measurable success metrics, draws scope
boundaries, recommends an MVP slice, and produces Given-When-Then
acceptance criteria. The resulting ``ProductSpec`` is consumed by
downstream agents (planner/tester/reviewer) via ``SDLCState.product_spec``.

Architecture notes
-------------------
- Mirrors :mod:`planner_node` structure (factory + closure over ``ctx``).
- Uses :func:`call_llm_structured` with ``ProductSpec`` as response_format
  — no tool binding in this first version. Grounding comes from
  ``project_context`` (which already carries Goal history at the API
  layer) and ``long_term_memory``. Tool binding (``list_directory`` /
  ``search_code``) is deferred to a later phase to avoid the
  ``bind_tools`` + ``with_structured_output`` compatibility risk and
  the tool-call dispatch loop complexity (see ``coder_node`` for that
  pattern).
- Fallback is NOT ``build_error_command`` — that helper does not write
  ``product_spec``, which would leave the field empty and cause
  ``_force_route`` to dead-loop (requirements && !product_spec → po →
  fail → supervisor → po → ...). Instead, the except block writes a
  ``{"fallback": True, ...}`` placeholder so the guard advances to
  planner with raw requirements.
"""

from langgraph.graph.state import Command

from app.core.langgraph.agents.context import (
    AgentContext,
    call_llm_structured,
)
from app.core.langgraph.agents.supervisor_node import NodeCallable
from app.core.logging import logger
from app.core.metrics import sdlc_agent_errors_total
from app.core.prompts import load_agent_prompt
from app.schemas import ProductSpec, SDLCState
from app.utils import prepare_sdlc_messages


def make_po_node(ctx: AgentContext) -> NodeCallable:
    """Create the PO (Product Owner) node function.

    The returned callable has signature ``(state) -> Command``, matching
    LangGraph's ``add_node`` contract (no config parameter needed, same
    as ``planner_node``).
    """

    async def po_node(state: SDLCState) -> Command:
        system_prompt = load_agent_prompt(
            "product_manager",
            requirements=state.get("requirements", ""),
            long_term_memory=state.get("long_term_memory", ""),
            project_context=state.get("project_context", ""),
            image_contexts=state.get("image_contexts", []),
        )
        messages = prepare_sdlc_messages(state["messages"], system_prompt)

        try:
            response = await call_llm_structured(ctx, "po", "specing", messages, ProductSpec)

            spec_data = response.model_dump()
            logger.info(
                "sdlc_po_completed",
                acceptance_criteria_count=len(spec_data.get("acceptance_criteria", [])),
                needs_clarification=spec_data.get("needs_clarification", False),
                has_low_value_rationale=spec_data.get("low_value_rationale") is not None,
            )

            # Risk gate: if PO flags needs_clarification or low_value_rationale,
            # set human_approval_needed to pause workflow before coding starts.
            # supervisor_node detects this flag and triggers langgraph_interrupt.
            needs_clarification = spec_data.get("needs_clarification", False)
            low_value = spec_data.get("low_value_rationale")
            has_low_value = low_value is not None and str(low_value).strip() != ""

            update_dict: dict = {
                "messages": [{"role": "assistant", "content": response.summary or "Product spec generated."}],
                "product_spec": [spec_data],
                "status": "specing",
            }

            if needs_clarification or has_low_value:
                update_dict["human_approval_needed"] = True
                logger.info(
                    "po_risk_gate_triggered",
                    needs_clarification=needs_clarification,
                    has_low_value=has_low_value,
                    low_value_rationale=str(low_value)[:200] if low_value else None,
                )

            return Command(
                update=update_dict,
                goto="supervisor",
            )
        except Exception as e:
            # Critical: do NOT use build_error_command here — it does not
            # write product_spec, which would leave the field empty and
            # cause _force_route to dead-loop (requirements && !product_spec
            # → po → fail → supervisor → po → ...). Instead write a fallback
            # placeholder so the guard advances to planner with raw
            # requirements. The planner prompt handles empty/fallback spec
            # by falling back to {requirements}.
            sdlc_agent_errors_total.labels(agent_name="po", step="specing").inc()
            logger.error("sdlc_agent_parsing_failed", agent_name="po", error=str(e))
            fallback_spec = {
                "fallback": True,
                "summary": "PO unavailable — falling back to raw requirements.",
                "problem_statement": "",
                "target_users": [],
                "success_metrics": [],
                "in_scope": [],
                "out_of_scope": [],
                "assumptions": [],
                "risks": [],
                "acceptance_criteria": [],
                "mvp_recommendation": "",
                "open_questions": [],
                "needs_clarification": False,
                "low_value_rationale": None,
            }
            return Command(
                update={
                    "messages": [
                        {"role": "assistant", "content": f"PO error: {str(e)}. Proceeding with raw requirements."}
                    ],
                    "errors": [{"agent_name": "po", "content": str(e), "severity": "high"}],
                    "product_spec": [fallback_spec],
                    "status": "specing",
                },
                goto="supervisor",
            )

    return po_node
