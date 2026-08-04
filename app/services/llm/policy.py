"""Deterministic capability policy for configured language models.

The policy deliberately answers only one question: may a model be used for a
given agent purpose?  Provider availability and retry behaviour remain the
responsibility of :mod:`app.services.llm.service`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings


LOCAL_ONLY_PURPOSES = frozenset({"session_naming", "chat_intent", "chat_casual"})


@dataclass(frozen=True)
class ModelIdentity:
    """Stable, user-facing identity for one configured model name."""

    requested_model: str
    actual_model: str
    deployment: str


class ModelPolicy:
    """Allow local small models only for explicitly low-risk purposes."""

    def get_identity(self, model_name: str) -> ModelIdentity:
        """Return the stable physical identity and deployment of a model alias."""
        deployment = "local" if model_name in settings.LOCAL_LLM_MODELS else "cloud"
        actual_model = settings.LLM_MODEL_IDENTITIES.get(model_name, model_name)
        return ModelIdentity(model_name, actual_model, deployment)

    def is_allowed(self, model_name: str, purpose: str) -> bool:
        """Return whether a model may serve the requested agent purpose."""
        if model_name not in settings.LOCAL_LLM_MODELS:
            return True
        return purpose in LOCAL_ONLY_PURPOSES

    def validate(self, model_name: str, purpose: str) -> None:
        """Raise ``ValueError`` when a model-purpose pairing is prohibited."""
        if self.is_allowed(model_name, purpose):
            return
        raise ValueError(
            f"local model '{model_name}' is not allowed for purpose '{purpose}'; use a cloud-capable model"
        )

    def coder_models(self, model_names: list[str]) -> list[str]:
        """Return configured coder choices that satisfy the policy."""
        return [model_name for model_name in model_names if self.is_allowed(model_name, "coder")]


model_policy = ModelPolicy()
