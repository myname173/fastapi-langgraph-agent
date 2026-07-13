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
            max_tokens=settings.MAX_TOKENS,
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
        return [{"role": "system", "content": system_prompt}] + messages

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

    return [{"role": "system", "content": system_prompt}] + trimmed


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
