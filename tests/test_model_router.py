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
    """Tests for ModelRouter.get_model_for().

    Assertions are derived from ``ROUTING_TABLE`` rather than hard-coded model
    names: the table is provider-swappable (it is overridden at import time from
    the ``MODEL_ROUTING_TABLE`` env var), so pinning literal model names here
    turns every provider migration into a false test failure.
    """

    def test_each_sdlc_role_returns_its_routed_model(self):
        """Every SDLC role resolves to exactly the model in ROUTING_TABLE."""
        router = ModelRouter()
        for role in ("supervisor", "planner", "coder", "tester", "reviewer"):
            assert router.get_model_for(role) == ROUTING_TABLE[role]

    def test_non_sdlc_roles_return_their_routed_model(self):
        """Chat / session_naming also resolve through the routing table."""
        router = ModelRouter()
        for role in ("chat", "session_naming"):
            assert router.get_model_for(role) == ROUTING_TABLE[role]

    def test_routed_models_are_non_empty_strings(self):
        """The table never routes a role to an empty/blank model id."""
        router = ModelRouter()
        for role in ROUTING_TABLE:
            assert isinstance(router.get_model_for(role), str)
            assert router.get_model_for(role).strip()

    def test_unknown_role_falls_back_to_default(self):
        """Unknown role returns settings.DEFAULT_LLM_MODEL."""
        router = ModelRouter()
        with patch.object(settings, "DEFAULT_LLM_MODEL", "sentinel-model-x"):
            result = router.get_model_for("nonexistent_role")
            assert result == "sentinel-model-x"


class TestModelRouterGetModelForCall:
    """Tests for ModelRouter.get_model_for_call()."""

    def test_returns_none_when_routed_equals_default(self):
        """When routed model == default, returns None (omit model_name)."""
        router = ModelRouter()
        # Patch the default to whatever the supervisor actually routes to, so
        # the equality holds regardless of the configured provider.
        with patch.object(settings, "DEFAULT_LLM_MODEL", ROUTING_TABLE["supervisor"]):
            result = router.get_model_for_call("supervisor")
            assert result is None

    def test_returns_model_name_when_routed_differs_from_default(self):
        """When routed model != default, returns the model name."""
        router = ModelRouter()
        # A sentinel default can never equal a real routed model.
        with patch.object(settings, "DEFAULT_LLM_MODEL", "sentinel-model-x"):
            result = router.get_model_for_call("coder")
            assert result == ROUTING_TABLE["coder"]

    def test_unknown_role_returns_none_when_default_matches(self):
        """Unknown role → default model; if default == default, returns None."""
        router = ModelRouter()
        with patch.object(settings, "DEFAULT_LLM_MODEL", "sentinel-model-x"):
            # Unknown role → get_model_for returns the default ("sentinel-model-x")
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
