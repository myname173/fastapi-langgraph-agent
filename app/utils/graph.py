"""This file contains the graph utilities for the application."""

import tiktoken
from langchain_core.messages import BaseMessage
from langchain_core.messages import trim_messages as _trim_messages

from app.core.config import settings
from app.core.logging import logger

# Import Message from the leaf submodule (not the app.schemas package) to avoid
# a circular import: app.schemas.__init__ → app.schemas.auth → app.utils.sanitization
# → app.utils.__init__ → app.utils.graph → app.schemas (partially initialized).
# This was masked until the L-5 fix added the auth→utils edge; the app first
# reloaded on 2026-07-09 after L-5, exposing the cycle.
from app.schemas.chat import Message

# Cache tiktoken encoding at module level — thread-safe and reusable
try:
    _TIKTOKEN_ENCODING = tiktoken.encoding_for_model(settings.DEFAULT_LLM_MODEL)
except KeyError:
    _TIKTOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")


def _count_tokens_tiktoken(messages: list) -> int:
    """Count tokens locally using tiktoken — no API call needed."""
    num_tokens = 0
    for message in messages:
        # Every message has overhead tokens for role/name
        num_tokens += 4
        if isinstance(message, dict):
            for _, value in message.items():
                if isinstance(value, str):
                    num_tokens += len(_TIKTOKEN_ENCODING.encode(value))
        elif isinstance(message, BaseMessage):
            content = message.content
            if isinstance(content, str):
                num_tokens += len(_TIKTOKEN_ENCODING.encode(content))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, str):
                        num_tokens += len(_TIKTOKEN_ENCODING.encode(block))
                    elif isinstance(block, dict) and "text" in block:
                        num_tokens += len(_TIKTOKEN_ENCODING.encode(block["text"]))
    num_tokens += 2  # every reply is primed with assistant
    return num_tokens


def dump_messages(messages: list[Message]) -> list[dict]:
    """Dump the messages to a list of dictionaries.

    Args:
        messages (list[Message]): The messages to dump.

    Returns:
        list[dict]: The dumped messages.
    """
    return [message.model_dump() for message in messages]


def dump_sdlc_messages(messages: list[dict]) -> list[dict]:
    """Dump SDLC messages (already dicts) to a list of dictionaries.

    Since SDLC state stores messages as dicts, this function simply returns
    them as-is, maintaining compatibility with the llm_service.call() interface.

    Args:
        messages (list[dict]): The messages to dump.

    Returns:
        list[dict]: The messages (already in dict format).
    """
    return messages


def extract_text_content(content: str | list) -> str:
    """Extract plain text from an LLM content value.

    Handles both the simple string format and the structured block list returned
    by GPT-5 / Responses API models:
        [{'type': 'reasoning', ...}, {'type': 'text', 'text': '...'}]

    Args:
        content: Raw content from a LangChain BaseMessage.

    Returns:
        Plain text string (empty string when nothing extractable is present).
    """
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "reasoning":
                logger.debug(
                    "reasoning_block_received",
                    reasoning_id=block.get("id"),
                    has_summary=bool(block.get("summary")),
                )
    return "".join(parts)


def process_llm_response(response: BaseMessage) -> BaseMessage:
    """Normalise a raw LLM response so that ``response.content`` is always a plain string, regardless of the provider's content format.

    Args:
        response: The raw response from the LLM.

    Returns:
        The same BaseMessage instance with ``content`` set to a plain string.
    """
    if isinstance(response.content, list):
        response.content = extract_text_content(response.content)
        logger.debug(
            "processed_structured_content",
            content_block_count=len(response.content),
            extracted_length=len(response.content),
        )
    return response


def prepare_messages(messages: list[Message], system_prompt: str) -> list[Message]:
    """Prepare the messages for the LLM.

    Args:
        messages (list[Message]): The messages to prepare.
        system_prompt (str): The system prompt to use.

    Returns:
        list[Message]: The prepared messages.
    """
    try:
        trimmed_messages = _trim_messages(
            dump_messages(messages),
            strategy="last",
            token_counter=_count_tokens_tiktoken,
            max_tokens=settings.CHAT_CONTEXT_MAX_TOKENS,
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
    except ValueError as e:
        # Handle unrecognized content blocks (e.g., reasoning blocks from GPT-5)
        if "Unrecognized content block type" in str(e):
            logger.warning(
                "token_counting_failed_skipping_trim",
                error=str(e),
                message_count=len(messages),
            )
            # Skip trimming and return all messages
            trimmed_messages = messages
        else:
            raise

    return [Message(role="system", content=system_prompt)] + trimmed_messages


# Message "type"/"role" values grouped by how they participate in the
# tool-calling protocol. Used by :func:`sanitize_tool_call_history`.
_TOOL_CALLING_KINDS = frozenset({"ai", "assistant"})
_TOOL_RESULT_KINDS = frozenset({"tool"})
_TOOL_SCOPE_RESET_KINDS = frozenset({"human", "user", "system"})


def _message_kind(msg: object) -> str:
    """Return the normalised message kind for a dict or ``BaseMessage``."""
    if isinstance(msg, dict):
        return str(msg.get("type") or msg.get("role") or "")
    return str(getattr(msg, "type", "") or getattr(msg, "role", "") or "")


def _message_tool_calls(msg: object) -> list:
    """Return the assistant ``tool_calls`` list (empty when absent)."""
    if isinstance(msg, dict):
        return list(msg.get("tool_calls") or [])
    return list(getattr(msg, "tool_calls", None) or [])


def _tool_call_id_of(tool_call: object) -> str | None:
    """Return the ``id`` of a single tool call (dict or ``ToolCall``)."""
    if isinstance(tool_call, dict):
        return tool_call.get("id")
    return getattr(tool_call, "id", None)


def _message_tool_call_id(msg: object) -> str | None:
    """Return the ``tool_call_id`` of a tool-result message."""
    if isinstance(msg, dict):
        return msg.get("tool_call_id")
    return getattr(msg, "tool_call_id", None)


def _message_content_is_empty(msg: object) -> bool:
    """True when the message carries no textual content."""
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    return len(content) == 0


def sanitize_tool_call_history(messages: list) -> list:
    """Drop tool-call payloads the provider API would reject.

    OpenAI-compatible endpoints (including DeepSeek) reject a request when:

    * a message with role ``tool`` is not preceded by an assistant message
      declaring a matching ``tool_calls`` id (orphan tool result), or
    * an assistant message declares ``tool_calls`` that are never answered.

    Two code paths in the SDLC graph can produce such a state:

    1. Synthetic tool results that belong to no assistant tool call — e.g.
       the tester node force-invoking ``run_tests`` and tagging the result
       with a made-up ``tool_call_id`` (``"auto_run_tests"``).
    2. Context trimming (:func:`_trim_messages`) cutting an assistant
       ``tool_calls`` message away from the tool messages that answer it,
       or keeping a tool message whose declaring assistant was dropped.

    This normaliser makes the payload API-valid again by removing orphaned
    tool results and stripping unanswered ``tool_calls`` entries. Messages
    with no tool-call involvement pass through untouched.

    Args:
        messages: The raw conversation history (dicts or ``BaseMessage``).

    Returns:
        A history whose tool-call pairing is internally consistent.
    """
    if not messages:
        return messages

    # ---- Pass 1: determine which tool_call ids are legitimately answered.
    declared: set[str] = set()
    answered: set[str] = set()
    for msg in messages:
        kind = _message_kind(msg)
        if kind in _TOOL_CALLING_KINDS:
            declared = set()
            for tool_call in _message_tool_calls(msg):
                call_id = _tool_call_id_of(tool_call)
                if call_id:
                    declared.add(call_id)
        elif kind in _TOOL_RESULT_KINDS:
            call_id = _message_tool_call_id(msg)
            if call_id and call_id in declared:
                answered.add(call_id)
        elif kind in _TOOL_SCOPE_RESET_KINDS:
            declared = set()

    # ---- Pass 2: rebuild a payload the API will accept.
    cleaned: list = []
    dropped_tool_results = 0
    stripped_tool_calls = 0
    for msg in messages:
        kind = _message_kind(msg)

        if kind in _TOOL_RESULT_KINDS:
            call_id = _message_tool_call_id(msg)
            if not call_id or call_id not in answered:
                dropped_tool_results += 1
                continue
            cleaned.append(msg)
            continue

        if kind in _TOOL_CALLING_KINDS:
            tool_calls = _message_tool_calls(msg)
            if tool_calls:
                kept = [tc for tc in tool_calls if _tool_call_id_of(tc) in answered]
                if len(kept) != len(tool_calls):
                    stripped_tool_calls += len(tool_calls) - len(kept)
                    if not kept and _message_content_is_empty(msg):
                        # Nothing left to convey — drop the empty shell.
                        continue
                    if isinstance(msg, dict):
                        msg = {**msg, "tool_calls": kept}
                    else:
                        msg = msg.model_copy(update={"tool_calls": kept})
            cleaned.append(msg)
            continue

        cleaned.append(msg)

    if dropped_tool_results or stripped_tool_calls:
        logger.warning(
            "sdlc_tool_call_history_sanitized",
            dropped_tool_results=dropped_tool_results,
            stripped_tool_calls=stripped_tool_calls,
            original_count=len(messages),
            sanitized_count=len(cleaned),
        )
    return cleaned


def prepare_sdlc_messages(messages: list[dict], system_prompt: str) -> list[dict]:
    """Prepare messages for SDLC Orchestrator agents.

    Works with raw dicts instead of Pydantic Message objects, which is needed
    because LangGraph's add_messages reducer stores messages as dicts.

    When ``settings.ENABLE_SDLC_CONTEXT_TRIM`` is true (default), the
    conversation history is trimmed to ``settings.SDLC_MAX_TOKENS`` tokens
    using ``trim_messages(strategy="last")`` — mirroring the behaviour of
    :func:`prepare_messages` used by the chatbot path. This prevents
    multi-round SDLC workflows (supervisor→coder→tester→reviewer→…) from
    growing the message list unbounded and overflowing the LLM context
    window.

    The system prompt is **not** part of the trimmed payload: it is
    prepended after trimming, so it is always preserved in full. The
    trimmer keeps the most recent human/ai turns and drops older ones.

    Args:
        messages (list[dict]): The messages to prepare.
        system_prompt (str): The system prompt to use.

    Returns:
        list[dict]: The prepared messages with system prompt prepended.
    """
    if not settings.ENABLE_SDLC_CONTEXT_TRIM:
        return [{"role": "system", "content": system_prompt}] + sanitize_tool_call_history(messages)

    try:
        trimmed = _trim_messages(
            messages,
            strategy="last",
            token_counter=_count_tokens_tiktoken,
            max_tokens=settings.SDLC_MAX_TOKENS,
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
    except ValueError as e:
        # Handle unrecognized content blocks (e.g., reasoning blocks)
        if "Unrecognized content block type" in str(e):
            logger.warning(
                "sdlc_token_counting_failed_skipping_trim",
                error=str(e),
                message_count=len(messages),
            )
            trimmed = messages
        else:
            raise

    # Trimming may have split an assistant tool_calls message from its tool
    # results (or vice-versa). Normalise before the payload reaches the API.
    return [{"role": "system", "content": system_prompt}] + sanitize_tool_call_history(trimmed)


def code_changes_reducer(existing: list[dict], updates: list[dict]) -> list[dict]:
    """Custom reducer for code_changes field.

    Deduplicates changes by file path, with last-write-wins semantics.
    This prevents duplicate changes to the same file when multiple agents
    modify the same file.

    Args:
        existing: Current state of code_changes
        updates: New changes to merge

    Returns:
        Merged list of changes with deduplication
    """
    merged = {}
    for change in existing:
        key = change.get("file_path", str(id(change)))
        merged[key] = change
    for update in updates:
        key = update.get("file_path", str(id(update)))
        merged[key] = update
    return list(merged.values())


def replace_list_reducer(_existing: list[dict], updates: list[dict]) -> list[dict]:
    """Custom reducer that replaces the entire list.

    Used for fields like 'plan' where updates should completely replace
    the existing value rather than append to it.

    Args:
        _existing: Current state (ignored)
        updates: New list to replace with

    Returns:
        The new list
    """
    return updates
