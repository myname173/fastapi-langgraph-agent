"""Supervisor node — top-level SDLC workflow coordinator.

Routes to the appropriate sub-agent based on current state, tracks
progress, and determines completion.  Extracted from the former
``SupervisorGraph._supervisor_node`` method.
"""

from typing import Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.graph.state import Command
from langgraph.types import interrupt as langgraph_interrupt

from app.core.config import settings
from app.core.langgraph.agents.context import (
    AgentContext,
    build_error_command,
    call_llm_structured,
)
from app.core.logging import logger
from app.core.metrics import sdlc_workflow_completed_total
from app.core.prompts import load_agent_prompt
from app.schemas import SDLCState, SupervisorDecision
from app.utils import prepare_sdlc_messages

# LangGraph node callable type: async (state, config) -> Command
NodeCallable = Callable[..., Awaitable[Command]]


def make_supervisor_node(ctx: AgentContext) -> NodeCallable:
    """Create the supervisor node function, capturing ``ctx`` via closure.

    The returned callable has signature ``(state, config) -> Command``,
    matching LangGraph's ``add_node`` contract.
    """

    async def supervisor_node(state: SDLCState, config: RunnableConfig) -> Command:
        validation_results = state.get("validation_results", [])
        coder_retry_count = state.get("coder_retry_count", 0)
        requirements = state.get("requirements", "") or ""
        plan = state.get("plan", []) or []
        code_changes = state.get("code_changes", []) or []
        test_results = state.get("test_results", []) or []
        feedback = state.get("feedback", []) or []
        human_approval_needed = state.get("human_approval_needed", False)
        # PO agent's structured spec — empty until po_node runs.
        # _force_route uses (requirements && !product_spec) → po to enforce
        # the specing phase before planning. A fallback entry
        # {"fallback": True, ...} (written by po_node on failure) counts as
        # "present" so the guard advances to planner with raw requirements.
        product_spec = state.get("product_spec", []) or []

        # ------------------------------------------------------------------
        # C-7 + H-3 fix: human-in-the-loop approval gate. Both coder_node
        # and reviewer_node set human_approval_needed=True and return here;
        # their state updates (code_changes, feedback, security_issues,
        # human_approval_needed, artifacts) are already committed to the
        # checkpoint. Interrupt now so the user can approve/reject via
        # approve_task()/reject_task(), which clear the flag via
        # update_state(as_node="supervisor") then ainvoke(None) resumes —
        # supervisor re-runs with the flag False and falls through to
        # normal routing. Previously coder set the flag but never
        # interrupted, so the graph ran to END and the approval buttons
        # were a no-op (H-3).
        # ------------------------------------------------------------------
        if human_approval_needed:
            langgraph_interrupt("Human approval required before proceeding")

        # ------------------------------------------------------------------
        # M-8 + L-2 fix: circuit breaker for persistent agent failures.
        # If an agent (planner/reviewer/tester) keeps failing, the
        # _force_route guard below would loop back to it forever
        # (e.g. test_results present but feedback empty → force reviewer
        # → reviewer fails → no feedback → force reviewer → …). We count
        # consecutive errors per agent from the errors list; if any agent
        # exceeds MAX_CODER_RETRIES, we terminate the workflow with an
        # error status instead of looping until recursion_limit.
        # ------------------------------------------------------------------
        errors = state.get("errors", []) or []
        _agent_error_counts: dict[str, int] = {}
        for err in errors:
            name = err.get("agent_name", "") if isinstance(err, dict) else ""
            if name:
                _agent_error_counts[name] = _agent_error_counts.get(name, 0) + 1

        _max_retries = settings.MAX_CODER_RETRIES
        for agent, count in _agent_error_counts.items():
            if agent in ("planner", "coder", "tester", "reviewer") and count > _max_retries:
                logger.error(
                    "sdlc_supervisor_circuit_breaker_tripped",
                    agent_name=agent,
                    error_count=count,
                    max_retries=_max_retries,
                )
                sdlc_workflow_completed_total.labels(status="fail", task_type="general").inc()
                return Command(
                    update={
                        "is_complete": True,
                        # "failed", not "completed": the workflow gave up because
                        # an agent kept erroring, so it produced no usable output.
                        # Reporting "completed" here made a total failure look like
                        # success in the UI and in GET /tasks (the metric above
                        # already counted it as a failure).
                        "status": "failed",
                        "messages": [
                            {
                                "role": "assistant",
                                "content": (
                                    f"Workflow aborted: {agent} failed {count} times "
                                    f"(max {_max_retries}). Please retry or adjust requirements."
                                ),
                            }
                        ],
                    },
                    goto=END,
                )

        # ------------------------------------------------------------------
        # Layer 0: Hard state-machine guards (defend against LLM misrouting).
        # The supervisor LLM occasionally returns is_complete=True on a fresh
        # task, which would terminate the workflow with no plan/code/output.
        # These guards force the canonical SDLC order regardless of LLM output:
        #   requirements → po → planner → coder → tester → reviewer → completed
        # ------------------------------------------------------------------
        def _force_route() -> Command | None:
            # Block premature completion: a task is only eligible to end when
            # every stage has produced tangible output.
            if human_approval_needed:
                return None  # approval gating handled below
            # PO guard: requirements exist but no spec yet → produce spec first.
            # A fallback entry ({"fallback": True, ...}) counts as "present"
            # so PO failure does not dead-loop here.
            # C-9 fix: only force PO when the workflow has not already advanced
            # past it. If code/tests/feedback already exist, a missing spec is a
            # stale-state artifact (e.g. MemorySaver checkpoint not retaining the
            # PO write) — forcing PO again would dead-loop tester→supervisor→po.
            # In that case fall through to the later guards / normal routing.
            _advanced_past_po = bool(plan or code_changes or test_results or feedback)
            if requirements.strip() and not product_spec and not _advanced_past_po:
                logger.warning(
                    "sdlc_supervisor_guard_force_po",
                    reason="requirements_present_spec_empty",
                )
                return Command(update={"status": "specing"}, goto="po")
            # Planner guard: spec exists but no plan → plan from spec.
            # Falls through to coder guard when plan is already present.
            if product_spec and not plan:
                logger.warning(
                    "sdlc_supervisor_guard_force_planner",
                    reason="spec_present_plan_empty",
                )
                return Command(update={"status": "planning"}, goto="planner")
            if plan and not code_changes:
                logger.warning(
                    "sdlc_supervisor_guard_force_coder",
                    reason="plan_present_code_empty",
                )
                return Command(update={"status": "coding"}, goto="coder")
            if code_changes and not test_results:
                logger.warning(
                    "sdlc_supervisor_guard_force_tester",
                    reason="code_present_tests_empty",
                )
                return Command(update={"status": "testing"}, goto="tester")
            if test_results and not feedback:
                # Reviewer may legitimately return empty feedback when the
                # code is correct and has no issues. Without a retry cap the
                # guard would loop forever (reviewer → empty feedback → force
                # reviewer → …). Cap at MAX_CODER_RETRIES; once exhausted,
                # treat empty feedback as "approved with no issues" and let
                # the workflow complete. Mirrors the coder_retry_count pattern.
                reviewer_retry_count = state.get("reviewer_retry_count", 0)
                if reviewer_retry_count >= _max_retries:
                    logger.warning(
                        "sdlc_supervisor_guard_reviewer_retry_exhausted",
                        retry_count=reviewer_retry_count,
                        max_retries=_max_retries,
                    )
                    return None
                logger.warning(
                    "sdlc_supervisor_guard_force_reviewer",
                    reason="tests_present_feedback_empty",
                    retry_count=reviewer_retry_count,
                )
                return Command(
                    update={
                        "status": "reviewing",
                        "reviewer_retry_count": reviewer_retry_count + 1,
                    },
                    goto="reviewer",
                )
            return None

        # Layer 0a: auto-retry coder on validation failure (existing logic).
        if validation_results and not validation_results[-1].get("passed"):
            if coder_retry_count < settings.MAX_CODER_RETRIES:
                logger.info(
                    "sdlc_supervisor_auto_retry",
                    retry_count=coder_retry_count,
                    max_retries=settings.MAX_CODER_RETRIES,
                    validation_message=validation_results[-1].get("message", ""),
                )
                return Command(
                    update={
                        "status": "coding",
                        "feedback": [
                            {
                                "agent_name": "system",
                                "content": (
                                    f"Validation failed: {validation_results[-1].get('message', '')}. "
                                    f"Please fix the issues. Attempt {coder_retry_count + 1}/{settings.MAX_CODER_RETRIES}"
                                ),
                                "severity": "high",
                            }
                        ],
                    },
                    goto="coder",
                )

        system_prompt = load_agent_prompt(
            "supervisor",
            requirements=requirements,
            product_spec=product_spec,
            plan=plan,
            code_changes=code_changes,
            test_results=test_results,
            feedback=feedback,
            errors=state.get("errors", []),
            artifacts=state.get("artifacts", []),
            status=state.get("status", ""),
            human_approval_needed=human_approval_needed,
            validation_results=validation_results,
            coder_retry_count=coder_retry_count,
        )
        messages = prepare_sdlc_messages(state["messages"], system_prompt)

        try:
            response = await call_llm_structured(ctx, "supervisor", "routing", messages, SupervisorDecision)
        except Exception as e:
            return build_error_command("supervisor", "routing", state.get("status", "routing"), e)

        # ------------------------------------------------------------------
        # Layer 0b: apply hard guards BEFORE honouring LLM's completion flag.
        # If the LLM says "complete" but mandatory stages have no output,
        # the guard overrides and forces the next missing stage instead.
        # This is the single most important fix for "task completed but no
        # output" — it prevents premature termination on a fresh task.
        # ------------------------------------------------------------------
        if response.is_complete or response.next_agent == "END":
            forced = _force_route()
            if forced is not None:
                return forced
            session_id = config.get("configurable", {}).get("thread_id")
            logger.info("sdlc_workflow_completed", session_id=session_id)
            sdlc_workflow_completed_total.labels(status="success", task_type="general").inc()
            return Command(
                update={"is_complete": True, "status": "completed"},
                goto=END,
            )

        # ------------------------------------------------------------------
        # Layer 0c: defend against the LLM looping coder<->tester after code
        # has been written and tested but the reviewer hasn't weighed in. In
        # that state the canonical next stage is the reviewer (bounded by
        # reviewer_retry_count inside _force_route), not re-entering coding/
        # testing. Without this, a failing test runner (e.g. sandbox unavailable)
        # makes the supervisor trust the LLM's "go fix it in coder" decision
        # repeatedly, spinning coder<->tester until recursion_limit.
        # ------------------------------------------------------------------
        if response.next_agent in ("coder", "tester") and code_changes and test_results and not feedback:
            forced = _force_route()
            if forced is not None:
                return forced

        # ------------------------------------------------------------------
        # Layer 0d: anti-loop guard. If the LLM routes back to an agent whose
        # canonical stage is ALREADY complete (e.g. planner already produced a
        # plan, coder already produced code), trusting that routing would spin
        # planner->supervisor->planner forever until recursion_limit (seen on
        # the security_repair_sqli task with an unstable model). Instead, when
        # the LLM points at a finished stage, ignore it and advance to the next
        # still-incomplete stage; if all stages are done, force completion.
        # ------------------------------------------------------------------
        _stage_complete = {
            "po": bool(product_spec),
            "planner": bool(plan),
            "coder": bool(code_changes),
            "tester": bool(test_results),
            "reviewer": bool(feedback),
        }
        _llm_target = response.next_agent
        if _llm_target in _stage_complete and _stage_complete.get(_llm_target):
            logger.warning(
                "sdlc_supervisor_anti_loop_override",
                llm_target=_llm_target,
                reason="stage_already_complete",
            )
            forced = _force_route()
            if forced is not None:
                return forced
            # All mandatory stages done but LLM keeps looping a finished stage:
            # force workflow completion instead of exhausting recursion_limit.
            session_id = config.get("configurable", {}).get("thread_id")
            logger.info("sdlc_workflow_completed", session_id=session_id)
            sdlc_workflow_completed_total.labels(status="success", task_type="general").inc()
            return Command(
                update={"is_complete": True, "status": "completed"},
                goto=END,
            )

        # LLM picked a non-END next_agent — trust it (normal routing / retry).

        logger.info(
            "sdlc_routing_decision",
            next_agent=response.next_agent,
            status_update=response.status_update,
            reasoning=response.reasoning,
            session_id=config.get("configurable", {}).get("thread_id"),
        )

        update = {"status": response.status_update or state["status"]}
        return Command(update=update, goto=response.next_agent)

    return supervisor_node
