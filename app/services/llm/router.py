"""Dynamic model router — selects the optimal model per agent role.

The :class:`LLMService` circular fallback is a **fault-tolerance** mechanism
(try the next model if the current one fails).  This module adds a
**capability-matching** layer on top: route each agent role to the most
cost-effective model that can handle its task.

Hybrid routing table (local vLLM + cloud DashScope):

    =============== ================= ======== ============================
    Agent role      Model              Tier     Rationale
    =============== ================= ======== ============================
    supervisor      qwen3.7-plus       medium   Complex state analysis +
                                              routing decision; 0.5B model
                                              tested but cannot understand
                                              multi-field SDLC state
    po              qwen3.7-max-preview heavy   Product Owner: WHY/ROI/MVP
                                              reasoning + structured spec
                                              with Given-When-Then ACs.
                                              Same tier as planner — both
                                              need strong reasoning for
                                              downstream agent consumption.
    planner         qwen3.7-max-preview heavy   Complex planning
    coder           qwen3-coder-plus   coding   Coding specialist
    tester          qwen3.7-plus       medium   Test execution
    reviewer        qwen3.7-max-2026-  heavy    Quality review
                    06-08
    session_naming  local-qwen-0.5b    local    Title generation (short output,
                                              single-field state) — 0.5B
                                              model verified sufficient
    chat            qwen3.7-max-preview heavy   General-purpose (default)
    =============== ================= ======== ============================

Only ``session_naming`` uses the local vLLM (Qwen2.5-0.5B on GTX 1650 Ti)
because it requires only short structured output from a single user message.
``supervisor`` was tested with 0.5B but cannot correctly read multi-field
SDLC state (requirements + plan + code + tests + feedback), so it stays
on cloud.

If the local vLLM is unavailable, ``LLMService``'s circular fallback
automatically degrades to the next model in ``LLM_MODELS`` (cloud), so
the workflow remains functional.

Heavyweight roles (planner, coder, tester, reviewer) use cloud models
because the local GPU (4GB VRAM) cannot run larger models.

Integration
-----------

The router does **not** replace the fallback chain — it selects the
*preferred* starting model.  If that model fails, ``LLMService``'s circular
fallback still iterates through ``LLMRegistry.LLMS`` for fault tolerance.

Models not in the original ``LLM_MODELS`` env var are registered on-demand
via :meth:`LLMRegistry.ensure_model` so the fallback chain can reach them.
"""

from typing import Optional

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.registry import LLMRegistry


# Agent role → preferred model name.
# Only session_naming uses local vLLM (local-qwen-0.5b) — verified
# sufficient for short structured output. Supervisor stays on cloud
# because 0.5B cannot understand multi-field SDLC state.
ROUTING_TABLE: dict[str, str] = {
    "supervisor": "qwen3.7-plus",
    "po": "qwen3.7-max-preview",
    "planner": "qwen3.7-max-preview",
    "coder": "qwen3-coder-plus",
    "tester": "qwen3.7-plus",
    "reviewer": "qwen3.7-max-2026-06-08",
    "session_naming": "local-qwen-0.5b",
    "chat": "qwen3.7-max-preview",
}


class ModelRouter:
    """Selects the preferred model for a given agent role.

    The routing table is a class-level constant, but the selected model
    name is resolved at call time so ``settings.DEFAULT_LLM_MODEL`` changes
    take effect without restarting the router instance.
    """

    def __init__(self) -> None:
        """Initialize the router and ensure all routed models are registered."""
        registered = LLMRegistry.get_all_names()
        for model_name in set(ROUTING_TABLE.values()):
            if model_name not in registered:
                LLMRegistry.ensure_model(model_name)
                logger.info("model_router_registered_model", model_name=model_name)
        logger.info(
            "model_router_initialized",
            routing_entries=len(ROUTING_TABLE),
            total_models=len(LLMRegistry.get_all_names()),
        )

    def get_model_for(self, agent_role: str) -> str:
        """Return the preferred model name for an agent role.

        Falls back to ``settings.DEFAULT_LLM_MODEL`` when the role is not
        in the routing table.

        Args:
            agent_role: One of ``"supervisor"``, ``"po"``, ``"planner"``,
                ``"coder"``, ``"tester"``, ``"reviewer"``, ``"session_naming"``,
                ``"chat"``.

        Returns:
            Model name string suitable for ``LLMService.call(model_name=...)``
            or ``LLMService.get_llm_for(...)``.
        """
        model_name = ROUTING_TABLE.get(agent_role)
        if model_name is None:
            logger.warning("model_router_unknown_role", agent_role=agent_role)
            return settings.DEFAULT_LLM_MODEL
        return model_name

    def get_model_for_call(self, agent_role: str) -> Optional[str]:
        """Return the model name for ``LLMService.call(model_name=...)``.

        Returns ``None`` when the routed model is the same as the default
        (so the caller can omit ``model_name`` and use the default path,
        which is slightly faster — no registry lookup).

        Args:
            agent_role: Agent role string.

        Returns:
            Model name, or ``None`` if the default model should be used.
        """
        model_name = self.get_model_for(agent_role)
        if model_name == settings.DEFAULT_LLM_MODEL:
            return None
        return model_name


# Module-level singleton — built once on first import.
model_router: ModelRouter = ModelRouter()
