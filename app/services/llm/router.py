"""Dynamic model router — selects the optimal model per agent role.

The :class:`LLMService` circular fallback is a **fault-tolerance** mechanism
(try the next model if the current one fails).  This module adds a
**capability-matching** layer on top: route each agent role to the most
cost-effective model that can handle its task.

Routing table (defaults — override with the MODEL_ROUTING_TABLE env var):

    =============== ================== ====== ==================================
    Agent role      Model              Tier   Rationale
    =============== ================== ====== ==================================
    supervisor      deepseek-v4-pro    heavy  Multi-field SDLC state analysis +
                                              routing decision
    po              deepseek-v4-pro    heavy  WHY/ROI/MVP reasoning + structured
                                              spec with Given-When-Then ACs
    planner         deepseek-v4-pro    heavy  Complex planning
    coder           deepseek-flash     fast   Code generation (1M-token context)
    tester          deepseek-flash     fast   Test execution
    reviewer        deepseek-v4-pro    heavy  Quality review
    session_naming  deepseek-flash     fast   Title generation (short output)
    chat            deepseek-flash     fast   General-purpose (default)
    =============== ================== ====== ==================================

2026-10-06: the chat provider moved from Alibaba Bailian to DeepSeek, so every
role now targets a DeepSeek model. ``MODEL_ROUTING_TABLE`` (a JSON object) can
override any entry without a code change — e.g. to put ``local-qwen-0.5b`` back
on ``session_naming`` when the local vLLM is running.

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

import json
import os
from typing import Optional

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.registry import LLMRegistry


# Agent role → preferred model name.
#
# 2026-10-06: the chat provider moved from Alibaba Bailian to DeepSeek
# (CHAT_BASE_URL=https://api.deepseek.com). Every cloud role must therefore
# name a model DeepSeek actually serves — a leftover Bailian name such as
# ``qwen3.8-max`` 404s on api.deepseek.com and burns a failed call before
# the fallback chain kicks in.
#
# Override per-deployment with the MODEL_ROUTING_TABLE env var (JSON object),
# e.g. MODEL_ROUTING_TABLE={"coder":"deepseek-v4-pro","chat":"deepseek-flash"}.
# Keep this in sync with CHAT_BASE_URL / LLM_MODELS.
_DEFAULT_ROUTING_TABLE: dict[str, str] = {
    "supervisor": "deepseek-v4-pro",
    "po": "deepseek-v4-pro",
    "planner": "deepseek-v4-pro",
    "coder": "deepseek-flash",
    "tester": "deepseek-flash",
    "reviewer": "deepseek-v4-pro",
    "session_naming": "deepseek-flash",
    "chat": "deepseek-flash",
}


def _load_routing_table() -> dict[str, str]:
    """Return the effective routing table, merged with the env override.

    ``MODEL_ROUTING_TABLE`` is an optional JSON object mapping agent role →
    model name. Known roles are overridden, unknown roles are added. A
    malformed value is logged and ignored (defaults are kept) so a typo
    cannot take the service down.

    Returns:
        The effective agent-role → model-name mapping.
    """
    table = dict(_DEFAULT_ROUTING_TABLE)
    raw = os.getenv("MODEL_ROUTING_TABLE", "").strip()
    if not raw:
        return table
    try:
        override = json.loads(raw)
        if not isinstance(override, dict):
            raise TypeError("MODEL_ROUTING_TABLE must be a JSON object")
        table.update({str(k): str(v) for k, v in override.items()})
        logger.info("model_routing_table_overridden", roles=sorted(override.keys()))
    except (ValueError, TypeError) as exc:
        logger.warning("model_routing_table_override_ignored", error=str(exc), raw=raw[:200])
    return table


ROUTING_TABLE: dict[str, str] = _load_routing_table()


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
