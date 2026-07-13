"""Code index tools for semantic search over workspace code.

Provides LangChain tools for building and searching a vector index of
code files in the workspace sandbox.
"""

from typing import List, Optional

from langchain_core.tools import tool

from app.core.langgraph.tools.sdlc_tools import get_workspace_path
from app.core.logging import logger
from app.services.code_index_service import code_index_service
from app.services.knowledge_graph_service import knowledge_graph_service
from app.services.smart_search_service import smart_search_service


@tool
async def search_code_index(
    thread_id: str,
    query: str,
    limit: int = 10,
    file_pattern: Optional[str] = None,
    filter_function: Optional[str] = None,
    filter_class: Optional[str] = None,
) -> List[dict]:
    """Search the code index for relevant code snippets using semantic search.

    Use this tool BEFORE writing code to understand existing patterns,
    find related implementations, and locate files relevant to your task.
    Results include file paths, code content, similarity scores, and metadata
    (function names, class names, line numbers, etc.).

    Args:
        thread_id: The thread ID for workspace isolation
        query: Semantic search query describing what you need to find
        limit: Maximum number of results (default 10, max 20)
        file_pattern: Optional glob filter by file path (e.g. "*.py",
            "app/core/**/*.py", "*.tsx")
        filter_function: Optional function name to filter results
            (e.g. "get_user", "validate_token")
        filter_class: Optional class name to filter results
            (e.g. "AuthService", "CodeIndexService")

    Returns:
        List of search result dicts, each containing:
        - file: relative file path
        - content: matched code snippet with structured header
        - score: similarity score (0.0 - 1.0, higher = more relevant)
        - type: content type (e.g. "code")
        - metadata: dict with function, class, language, chunk_index, etc.
    """
    try:
        safe_limit = min(max(limit, 1), 20)
        results = await code_index_service.search(
            thread_id,
            query,
            limit=safe_limit,
            file_pattern=file_pattern,
            filter_function=filter_function,
            filter_class=filter_class,
        )
        logger.info(
            "code_index_search_tool",
            thread_id=thread_id,
            result_count=len(results),
            file_pattern=file_pattern or "none",
        )
        return results
    except Exception as e:
        logger.error("code_index_search_tool_failed", thread_id=thread_id, error=str(e))
        return []


@tool
async def build_code_index(thread_id: str) -> dict:
    """Build or rebuild the semantic code index for the workspace.

    Scans all code files in the workspace, chunks them, generates
    embeddings, and stores them in the vector database.

    Args:
        thread_id: The thread ID for workspace isolation

    Returns:
        Stats dict with file_count, chunk_count, and skipped count
    """
    try:
        workspace = str(get_workspace_path(thread_id, "."))
        stats = await code_index_service.build_index(workspace, thread_id)
        logger.info(
            "code_index_build_tool",
            thread_id=thread_id,
            file_count=stats.get("file_count", 0),
            chunk_count=stats.get("chunk_count", 0),
            skipped=stats.get("skipped", 0),
        )
        return stats
    except Exception as e:
        logger.error("code_index_build_tool_failed", thread_id=thread_id, error=str(e))
        return {"file_count": 0, "chunk_count": 0, "error": str(e)}


@tool
async def update_code_index_file(thread_id: str, file_path: str) -> dict:
    """Update the code index for a single file after modifications.

    Call this after writing or editing a file to keep the index
    up-to-date.

    Args:
        thread_id: The thread ID for workspace isolation
        file_path: Relative path of the file to reindex

    Returns:
        Stats dict with chunk_count and file_path
    """
    try:
        workspace = str(get_workspace_path(thread_id, "."))
        stats = await code_index_service.update_file(workspace, thread_id, file_path)
        logger.info(
            "code_index_update_tool",
            thread_id=thread_id,
            file_path=file_path,
        )
        return stats
    except Exception as e:
        logger.error(
            "code_index_update_tool_failed",
            thread_id=thread_id,
            file_path=file_path,
            error=str(e),
        )
        return {"chunk_count": 0, "error": str(e)}


@tool
async def build_knowledge_graph(thread_id: str) -> dict:
    """Build or rebuild the knowledge graph for the workspace.

    Analyzes all Python files to extract entities (File, Class, Function)
    and their relationships (imports, calls, inherits, contains).

    Args:
        thread_id: The thread ID for workspace isolation

    Returns:
        Stats dict with node_count, edge_count, file_count, and skipped count
    """
    try:
        workspace = str(get_workspace_path(thread_id, "."))
        stats = await knowledge_graph_service.build_graph(workspace, thread_id)
        logger.info(
            "knowledge_graph_build_tool",
            thread_id=thread_id,
            node_count=stats.get("node_count", 0),
            edge_count=stats.get("edge_count", 0),
        )
        return stats
    except Exception as e:
        logger.error("knowledge_graph_build_tool_failed", thread_id=thread_id, error=str(e))
        return {"node_count": 0, "edge_count": 0, "error": str(e)}


@tool
async def update_knowledge_graph_file(thread_id: str, file_path: str) -> dict:
    """Update the knowledge graph for a single file after modifications.

    Call this after writing or editing a file to keep the graph up-to-date.

    Args:
        thread_id: The thread ID for workspace isolation
        file_path: Relative path of the file to update in the graph

    Returns:
        Stats dict with node_count, edge_count, and file_path
    """
    try:
        workspace = str(get_workspace_path(thread_id, "."))
        stats = await knowledge_graph_service.update_file_graph(workspace, thread_id, file_path)
        logger.info(
            "knowledge_graph_update_tool",
            thread_id=thread_id,
            file_path=file_path,
        )
        return stats
    except Exception as e:
        logger.error(
            "knowledge_graph_update_tool_failed",
            thread_id=thread_id,
            file_path=file_path,
            error=str(e),
        )
        return {"node_count": 0, "edge_count": 0, "error": str(e)}


@tool
def find_impact(thread_id: str, entity_name: str, max_depth: int = 3) -> List[dict]:
    """Find all entities that would be impacted by modifying a given entity.

    Use this to understand the scope of changes before modifying code.
    Returns reverse dependency paths showing which functions/classes/files
    call or depend on the specified entity.

    Args:
        thread_id: The thread ID for workspace isolation
        entity_name: Name of the entity to analyze (function, class, or file name)
        max_depth: Maximum traversal depth (default 3)

    Returns:
        List of impact paths, each containing:
        - path: List of entities in the impact chain
        - depth: The depth of the path
    """
    try:
        results = knowledge_graph_service.find_impact(thread_id, entity_name, max_depth)
        logger.info(
            "find_impact_tool",
            thread_id=thread_id,
            entity_name=entity_name,
            impact_count=len(results),
        )
        return results
    except Exception as e:
        logger.error("find_impact_tool_failed", thread_id=thread_id, error=str(e))
        return []


@tool
def get_dependency_chain(thread_id: str, entry_point: str, max_depth: int = 3) -> List[dict]:
    """Get the dependency chain from an entry point entity.

    Use this to understand what an entity depends on - shows imports,
    called functions, and inherited classes.

    Args:
        thread_id: The thread ID for workspace isolation
        entry_point: Name of the entry point entity (function, class, or file name)
        max_depth: Maximum traversal depth (default 3)

    Returns:
        List of dependency paths, each containing:
        - path: List of entities in the dependency chain
        - depth: The depth of the path
    """
    try:
        results = knowledge_graph_service.get_dependency_chain(thread_id, entry_point, max_depth)
        logger.info(
            "get_dependency_chain_tool",
            thread_id=thread_id,
            entry_point=entry_point,
            chain_count=len(results),
        )
        return results
    except Exception as e:
        logger.error("get_dependency_chain_tool_failed", thread_id=thread_id, error=str(e))
        return []


@tool
def get_graph_summary(thread_id: str) -> dict:
    """Get a summary of the knowledge graph for the current workspace.

    Shows counts of nodes (files, classes, functions) and edges (imports,
    calls, inherits, contains).

    Args:
        thread_id: The thread ID for workspace isolation

    Returns:
        Summary dict with node counts, edge counts, and enabled status
    """
    try:
        summary = knowledge_graph_service.get_graph_summary(thread_id)
        logger.info(
            "get_graph_summary_tool",
            thread_id=thread_id,
            node_count=summary.get("node_count", 0),
        )
        return summary
    except Exception as e:
        logger.error("get_graph_summary_tool_failed", thread_id=thread_id, error=str(e))
        return {"error": str(e)}


@tool
async def smart_search(
    thread_id: str,
    query: str,
    limit: int = 10,
    file_pattern: Optional[str] = None,
    filter_function: Optional[str] = None,
    filter_class: Optional[str] = None,
    max_depth: int = 2,
) -> List[dict]:
    """Smart hybrid search combining vector search, knowledge graph, and keyword matching.

    This is the RECOMMENDED search tool for understanding codebase structure.
    It returns context-rich results with dependency information and summaries.

    Args:
        thread_id: The thread ID for workspace isolation
        query: Search query describing what you need to find
        limit: Maximum number of results (default 10)
        file_pattern: Optional glob filter by file path (e.g. "*.py", "app/core/**/*.py")
        filter_function: Optional function name to filter results
        filter_class: Optional class name to filter results
        max_depth: Maximum graph traversal depth for dependency analysis (default 2)

    Returns:
        List of search result dicts, each containing:
        - file: relative file path
        - content: matched code snippet with structured header
        - score: combined relevance score (0.0 - 1.0)
        - vector_score: vector similarity component
        - keyword_score: keyword match component
        - graph_score: graph dependency component
        - summary: relationship summary (e.g., "被 X 处调用; 依赖 Y")
        - source: list of sources contributing to this result
        - metadata: full metadata with function, class, language info
    """
    try:
        workspace = str(get_workspace_path(thread_id, "."))
        safe_limit = min(max(limit, 1), 20)
        results = await smart_search_service.smart_search(
            thread_id,
            query,
            workspace,
            limit=safe_limit,
            file_pattern=file_pattern,
            filter_function=filter_function,
            filter_class=filter_class,
            max_depth=max_depth,
        )
        logger.info(
            "smart_search_tool",
            thread_id=thread_id,
            query=query,
            result_count=len(results),
        )
        return results
    except Exception as e:
        logger.error("smart_search_tool_failed", thread_id=thread_id, error=str(e))
        return []


code_index_tools_list = [
    smart_search,
    search_code_index,
    build_code_index,
    update_code_index_file,
    build_knowledge_graph,
    update_knowledge_graph_file,
    find_impact,
    get_dependency_chain,
    get_graph_summary,
]
