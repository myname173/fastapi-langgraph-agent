"""LLM model registry with pre-initialized instances.

The registry is built dynamically from ``settings.LLM_MODELS`` so it works with
any OpenAI-compatible provider — OpenAI, Alibaba Bailian/DashScope, SiliconFlow,
Azure OpenAI, etc. Set ``OPENAI_BASE_URL`` and ``LLM_MODELS`` in the environment
to switch providers without changing code.

Per-model provider overrides (``settings.LLM_PROVIDER_OVERRIDES``) allow
individual models to use a different ``base_url``/``api_key``/``max_tokens``,
enabling hybrid routing (e.g. local vLLM + cloud API). Models without an
override fall back to the shared ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
``MAX_TOKENS``.

Provider-specific parameters (e.g. OpenAI ``reasoning``) should be passed via
``LLMService.call(**model_kwargs)`` per-call rather than baked into the registry,
so the registry stays provider-agnostic.
"""

from typing import (
    Any,
    Dict,
    List,
)

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.usage_tracker import usage_callback_handler


def _resolve_provider_config(model_name: str) -> tuple[SecretStr, str, Dict[str, Any]]:
    """Resolve provider config for a model, with per-model override support.

    Checks ``settings.LLM_PROVIDER_OVERRIDES`` first; falls back to the
    shared ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``MAX_TOKENS``.

    Args:
        model_name: Model identifier to look up.

    Returns:
        ``(api_key, base_url, token_limit)`` triple.
    """
    override = settings.LLM_PROVIDER_OVERRIDES.get(model_name, {})
    return (
        SecretStr(override.get("api_key", settings.OPENAI_API_KEY)),
        override.get("base_url", settings.OPENAI_BASE_URL),
        {"max_completion_tokens": override.get("max_tokens", settings.MAX_TOKENS)},
    )


def _build_llm(model_name: str) -> ChatOpenAI:
    """Construct a ChatOpenAI instance for the given model name.

    Binds the :data:`usage_callback_handler` so every call (including
    ``with_structured_output`` paths that hide the underlying
    ``BaseMessage``) is observed for token-usage / cost tracking.

    Args:
        model_name: Model identifier understood by the configured provider.

    Returns:
        A ChatOpenAI instance wired to the resolved api_key, base_url,
        token limit, and usage-tracking callback.
    """
    api_key, base_url, token_limit = _resolve_provider_config(model_name)
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        max_completion_tokens=token_limit["max_completion_tokens"],
        callbacks=[usage_callback_handler],
    )


class LLMRegistry:
    """Registry of available LLM models with pre-initialized instances.

    Models are loaded from ``settings.LLM_MODELS`` (a comma-separated env var).
    ``settings.DEFAULT_LLM_MODEL`` must appear in this list so the fallback
    chain in ``LLMService`` can locate the starting model.
    """

    LLMS: List[Dict[str, Any]] = [{"name": name, "llm": _build_llm(name)} for name in settings.LLM_MODELS]

    @classmethod
    def get(cls, model_name: str, **kwargs) -> BaseChatModel:
        """Get an LLM by name with optional argument overrides.

        When kwargs are provided a fresh ChatOpenAI instance is returned with
        those overrides applied, leaving the shared registry entry untouched.
        The shared ``api_key`` and ``base_url`` are always inherited from
        settings so the override path still targets the configured provider.

        Args:
            model_name: Name of the model to retrieve.
            **kwargs: Optional arguments to override default model configuration
                (e.g. ``temperature``, ``max_tokens``, ``reasoning`` for OpenAI
                o-series). Provider-incompatible kwargs will be rejected by the
                underlying SDK at call time.

        Returns:
            BaseChatModel instance.

        Raises:
            ValueError: If model_name is not found in LLMS.
        """
        model_entry = next((e for e in cls.LLMS if e["name"] == model_name), None)

        if not model_entry:
            available = ", ".join(e["name"] for e in cls.LLMS)
            raise ValueError(f"model '{model_name}' not found in registry. available models: {available}")

        if kwargs:
            logger.debug("creating_llm_with_custom_args", model_name=model_name, custom_args=list(kwargs.keys()))
            api_key, base_url, _ = _resolve_provider_config(model_name)
            return ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                callbacks=[usage_callback_handler],
                **kwargs,
            )

        logger.debug("using_default_llm_instance", model_name=model_name)
        return model_entry["llm"]

    @classmethod
    def get_all_names(cls) -> List[str]:
        """Return all registered model names in order.

        Returns:
            List of model name strings.
        """
        return [e["name"] for e in cls.LLMS]

    @classmethod
    def get_model_at_index(cls, index: int) -> Dict[str, Any]:
        """Return the model entry at a specific index, wrapping to 0 if out of range.

        Args:
            index: Index into LLMS.

        Returns:
            Model entry dict.
        """
        if 0 <= index < len(cls.LLMS):
            return cls.LLMS[index]
        return cls.LLMS[0]

    @classmethod
    def ensure_model(cls, model_name: str) -> None:
        """Ensure a model is registered, creating it on-demand if needed.

        Used by :class:`ModelRouter` to register routed models that are not
        in the original ``LLM_MODELS`` env var.  This extends the fallback
        chain so the circular fallback in ``LLMService`` can reach routed
        models.

        Args:
            model_name: Model identifier to register.
        """
        if not any(e["name"] == model_name for e in cls.LLMS):
            cls.LLMS.append({"name": model_name, "llm": _build_llm(model_name)})
            logger.info("model_registered_on_demand", model_name=model_name)
