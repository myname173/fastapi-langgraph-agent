"""Unit tests for ModelRouter — agent role → model selection.

Covers:
- Known agent roles return the expected model from ROUTING_TABLE
- Unknown agent roles fall back to settings.DEFAULT_LLM_MODEL
- get_model_for_call returns None when routed model equals default
- All routing table models are registered on init
"""

from unittest.mock import patch


from app.core.config import settings
from app.services.llm.router import (
    ROUTING_TABLE,
    ModelRouter,
)


class TestRoutingTable:
    """Tests for the ROUTING_TABLE constant."""

    def test_all_sdlc_roles_present(self):
        """All 5 SDLC agent roles are in the routing table."""
        for role in ("supervisor", "planner", "coder", "tester", "reviewer"):
            assert role in ROUTING_TABLE, f"missing role: {role}"

    def test_chat_and_session_naming_present(self):
        """Non-SDLC roles (chat, session_naming) are also routed."""
        assert "chat" in ROUTING_TABLE
        assert "session_naming" in ROUTING_TABLE

    def test_all_models_are_known_pricing_models(self):
        """Every routed model has a pricing entry (no under-reported costs)."""
        from app.services.llm.pricing import MODEL_PRICING_USD_PER_1M

        for role, model in ROUTING_TABLE.items():
            assert model in MODEL_PRICING_USD_PER_1M, f"role '{role}' routes to '{model}' which has no pricing entry"


class TestModelRouterGetModelFor:
    """Tests for ModelRouter.get_model_for()."""

    def test_supervisor_returns_medium_tier(self):
        """Supervisor → qwen3.7-plus (medium tier, simple routing)."""
        router = ModelRouter()
        assert router.get_model_for("supervisor") == "qwen3.7-plus"

    def test_planner_returns_heavy_tier(self):
        """Planner → qwen3.7-max-preview (heavy tier, complex planning)."""
        router = ModelRouter()
        assert router.get_model_for("planner") == "qwen3.7-max-preview"

    def test_coder_returns_coding_specialist(self):
        """Coder → qwen3-coder-plus (coding specialist)."""
        router = ModelRouter()
        assert router.get_model_for("coder") == "qwen3-coder-plus"

    def test_tester_returns_medium_tier(self):
        """Tester → qwen3.7-plus (medium tier, test execution)."""
        router = ModelRouter()
        assert router.get_model_for("tester") == "qwen3.7-plus"

    def test_reviewer_returns_heavy_tier(self):
        """Reviewer → qwen3.7-max-2026-06-08 (heavy tier, quality review)."""
        router = ModelRouter()
        assert router.get_model_for("reviewer") == "qwen3.7-max-2026-06-08"

    def test_unknown_role_falls_back_to_default(self):
        """Unknown role returns settings.DEFAULT_LLM_MODEL."""
        router = ModelRouter()
        with patch.object(settings, "DEFAULT_LLM_MODEL", "qwen3.7-plus"):
            result = router.get_model_for("nonexistent_role")
            assert result == "qwen3.7-plus"


class TestModelRouterGetModelForCall:
    """Tests for ModelRouter.get_model_for_call()."""

    def test_returns_none_when_routed_equals_default(self):
        """When routed model == default, returns None (omit model_name)."""
        router = ModelRouter()
        # Patch default to match the supervisor's routed model
        with patch.object(settings, "DEFAULT_LLM_MODEL", "qwen3.7-plus"):
            result = router.get_model_for_call("supervisor")
            assert result is None

    def test_returns_model_name_when_routed_differs_from_default(self):
        """When routed model != default, returns the model name."""
        router = ModelRouter()
        # Default is not qwen3-coder-plus (typically), so coder should return the name
        with patch.object(settings, "DEFAULT_LLM_MODEL", "qwen3.7-plus"):
            result = router.get_model_for_call("coder")
            assert result == "qwen3-coder-plus"

    def test_unknown_role_returns_none_when_default_matches(self):
        """Unknown role → default model; if default == default, returns None."""
        router = ModelRouter()
        with patch.object(settings, "DEFAULT_LLM_MODEL", "qwen3.7-plus"):
            # Unknown role → get_model_for returns "qwen3.7-plus" (the default)
            # Then get_model_for_call sees model == default → returns None
            result = router.get_model_for_call("unknown_role")
            assert result is None


class TestModelRouterInit:
    """Tests for ModelRouter.__init__ — model registration."""

    def test_init_registers_all_routed_models(self):
        """Init ensures every routed model is in LLMRegistry."""
        from app.services.llm.registry import LLMRegistry

        ModelRouter()  # Triggers ensure_model for all routed models (side effect)
        registered = LLMRegistry.get_all_names()
        for model in set(ROUTING_TABLE.values()):
            assert model in registered, f"routed model '{model}' not registered"

    def test_init_is_idempotent(self):
        """Creating a second router doesn't duplicate registry entries."""
        from app.services.llm.registry import LLMRegistry

        ModelRouter()
        count_after_first = len(LLMRegistry.LLMS)
        ModelRouter()  # Should not add duplicates
        count_after_second = len(LLMRegistry.LLMS)
        assert count_after_first == count_after_second
