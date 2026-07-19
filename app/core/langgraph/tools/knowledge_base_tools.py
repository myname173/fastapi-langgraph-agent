"""Knowledge base tools for semantic search over user-uploaded documents.

Provides a LangChain tool for the chat agent to search the user's
knowledge base (uploaded via /api/v1/knowledge-base/upload).

Gated by ENABLE_KNOWLEDGE_BASE feature flag in the tool registry.
The ``user_id`` is extracted from ``config["metadata"]["user_id"]``
(passed through by ``_tool_call``), NOT from LLM-supplied args, to
prevent cross-user data leakage.
"""

from typing import List

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from app.core.logging import logger
from app.services.knowledge_base_service import knowledge_base_service


@tool
async def search_knowledge_base(
    query: str,
    limit: int = 5,
    *,
    config: RunnableConfig,
) -> List[dict]:
    """Search your personal knowledge base for relevant information.

    Use this tool when the user asks about topics that may be covered by
    documents they've uploaded (e.g. investment knowledge, financial
    reports, tutorials, reference material). Returns the most relevant
    passages with source filenames.

    Args:
        query: Natural language search query describing what you need.
        limit: Maximum number of results (default 5, max 10).
        config: LangChain runtime config (auto-injected, not LLM-visible).
            Provides ``config["metadata"]["user_id"]`` for per-user isolation.

    Returns:
        List of matching passages, each containing content, score,
        filename, and doc_id. Empty list if no matches or KB disabled.
    """
    user_id = config.get("metadata", {}).get("user_id")
    if not user_id:
        return []

    safe_limit = min(max(limit, 1), 10)
    results = await knowledge_base_service.search(
        user_id=str(user_id),
        query=query,
        limit=safe_limit,
    )
    logger.info(
        "knowledge_base_search_tool",
        user_id=user_id,
        query=query,
        result_count=len(results),
    )
    return results


knowledge_base_tools_list = [search_knowledge_base]
