"""Unit tests for the model capability policy and execution audit hook."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from app.core.config import settings
from app.services.llm.execution_context import set_llm_execution_context
from app.services.llm.policy import model_policy
from app.services.llm.service import LLMService
from app.services.llm_execution_service import llm_execution_service


def test_local_model_is_limited_to_low_risk_purposes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local alias cannot be selected for SDLC or tool-backed work."""
    monkeypatch.setattr(settings, "LOCAL_LLM_MODELS", ["local-small"])

    assert model_policy.is_allowed("local-small", "chat_casual") is True
    assert model_policy.is_allowed("local-small", "session_naming") is True
    assert model_policy.is_allowed("local-small", "coder") is False
    assert model_policy.is_allowed("cloud-model", "coder") is True


def test_identity_exposes_physical_model_behind_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit records distinguish a vLLM alias from the loaded weights."""
    monkeypatch.setattr(settings, "LOCAL_LLM_MODELS", ["local-small"])
    monkeypatch.setattr(settings, "LLM_MODEL_IDENTITIES", {"local-small": "Qwen2.5-1.5B-GPTQ"})

    identity = model_policy.get_identity("local-small")

    assert identity.deployment == "local"
    assert identity.actual_model == "Qwen2.5-1.5B-GPTQ"


def test_coder_choices_exclude_local_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same policy used by API validation filters the model dropdown."""
    monkeypatch.setattr(settings, "LOCAL_LLM_MODELS", ["local-small"])

    assert model_policy.coder_models(["local-small", "cloud-coder"]) == ["cloud-coder"]


async def test_service_rejects_local_model_for_coder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Policy enforcement remains effective if a caller bypasses the UI."""
    monkeypatch.setattr(settings, "LOCAL_LLM_MODELS", ["local-small"])
    with patch("app.services.llm.service.LLMRegistry") as registry:
        registry.LLMS = [{"name": "local-small", "llm": MagicMock()}]
        registry.get_all_names.return_value = ["local-small"]
        service = LLMService()

        with set_llm_execution_context(
            execution_id="task-1",
            user_id=1,
            agent_name="coder",
            purpose="coder",
        ):
            with pytest.raises(ValueError, match="not allowed"):
                await service.call([{"role": "user", "content": "implement feature"}], model_name="local-small")


async def test_successful_attempt_writes_content_free_audit_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful call records identity and timing, never prompt content."""
    monkeypatch.setattr(settings, "LOCAL_LLM_MODELS", [])
    monkeypatch.setattr(settings, "DEFAULT_LLM_MODEL", "cloud-model")
    expected = AIMessage(content="ok")
    with patch("app.services.llm.service.LLMRegistry") as registry:
        registry.LLMS = [{"name": "cloud-model", "llm": MagicMock()}]
        registry.get_all_names.return_value = ["cloud-model"]
        registry.get.return_value = MagicMock()
        service = LLMService()

        with (
            patch.object(service, "_invoke_with_retry", new=AsyncMock(return_value=expected)),
            patch.object(llm_execution_service, "record", new=AsyncMock()) as record,
            set_llm_execution_context(
                execution_id="task-1",
                user_id=7,
                agent_name="planner",
                purpose="planner",
            ),
        ):
            result: Any = await service.call([{"role": "user", "content": "secret requirement"}])

    assert result == expected
    payload = record.await_args.kwargs
    assert payload["execution_id"] == "task-1"
    assert payload["requested_model"] == "cloud-model"
    assert payload["outcome"] == "succeeded"
    assert "content" not in payload
