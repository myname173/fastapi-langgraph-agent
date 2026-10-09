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
    shared ``CHAT_API_KEY`` / ``CHAT_BASE_URL`` / ``MAX_TOKENS``.

    Args:
        model_name: Model identifier to look up.

    Returns:
        ``(api_key, base_url, token_limit)`` triple.
    """
    override = settings.LLM_PROVIDER_OVERRIDES.get(model_name, {})
    token_limit: Dict[str, Any] = {"max_completion_tokens": override.get("max_tokens", settings.MAX_TOKENS)}
    # Thinking mode is a footgun for agentic use. It (a) refuses
    # tool_choice="required"/object — which breaks LangChain's
    #     function_calling structured-output method with a 400 error, and
    # (b) sometimes emits only the reasoning chain in `content`, leaving the
    #     JSON payload empty — which breaks json_mode parsing.
    # Disabling thinking yields stable, parseable structured output for the
    # SDLC agents. Non-thinking providers are left untouched.
    #
    # The parameter name is provider-specific — they are NOT interchangeable:
    #   DashScope qwen → `enable_thinking: bool`
    #   DeepSeek       → `thinking: {"type": "disabled"}` (verified 2026-10-06;
    #                    DeepSeek silently IGNORES DashScope's `enable_thinking`)
    if "qwen" in model_name.lower():
        # allow per-model override of thinking mode (some qwen snapshots e.g.
        # *-preview force enable_thinking=True and reject False with a 400).
        _thinking = override.get("enable_thinking", False)
        token_limit["extra_body"] = {"enable_thinking": _thinking}
    elif "deepseek" in model_name.lower():
        # DeepSeek defaults to thinking mode. LangChain does NOT replay
        # `reasoning_content` when it re-sends the conversation, so ANY
        # multi-turn tool-calling loop fails with:
        #   400 "The `reasoning_content` in the thinking mode must be passed
        #        back to the API."
        # That is exactly the SDLC coder/tester path (tool-calling agents),
        # so without this the coder fails on every turn and the supervisor
        # circuit breaker ends the run with zero code produced.
        # `reasoning_effort: "none"` is an equivalent alternative.
        # Override per model via LLM_PROVIDER_OVERRIDES if thinking is wanted
        # (only safe for single-shot, non-tool-calling calls).
        _thinking = override.get("thinking", {"type": "disabled"})
        token_limit["extra_body"] = {"thinking": _thinking}
    return (
        SecretStr(override.get("api_key", settings.CHAT_API_KEY)),
        override.get("base_url", settings.CHAT_BASE_URL),
        token_limit,
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
    # token_limit may carry extra_body (e.g. qwen thinking disable) alongside
    # max_completion_tokens.
    extra = {k: v for k, v in token_limit.items() if k != "max_completion_tokens"}
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        max_completion_tokens=token_limit["max_completion_tokens"],
        # S-hang fix: bound the underlying HTTP request so a misbehaving
        # provider (e.g. deepseek via DashScope) cannot hang forever.
        timeout=settings.LLM_REQUEST_TIMEOUT,
        max_retries=2,
        callbacks=[usage_callback_handler],
        **extra,
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
            api_key, base_url, token_limit = _resolve_provider_config(model_name)
            extra = {k: v for k, v in token_limit.items() if k != "max_completion_tokens"}
            return ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                max_completion_tokens=token_limit["max_completion_tokens"],
                # S-hang fix: bound the underlying HTTP request (see _build_llm).
                timeout=settings.LLM_REQUEST_TIMEOUT,
                max_retries=2,
                callbacks=[usage_callback_handler],
                **extra,
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
