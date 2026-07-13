"""Smart Search Service for hybrid code retrieval.

Combines vector search, knowledge graph traversal, and keyword matching
to provide intelligent, context-rich code search results.
"""

import fnmatch
import re
import time
from pathlib import Path
from typing import List, Optional, Dict, Any, Set, Tuple

from app.core.config import settings
from app.core.logging import logger
from app.services.code_index_service import code_index_service
from app.services.knowledge_graph_service import knowledge_graph_service
from app.utils.code_parser import collect_workspace_files


class SmartSearchService:
    """Service for hybrid code search combining multiple retrieval strategies."""

    def __init__(self):
        """Initialize the smart search service."""
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _clean_cache(self):
        """Clean expired cache entries."""
        now = time.time()
        expired_keys = [
            key
            for key, entry in self._cache.items()
            if now - entry["timestamp"] > settings.SMART_SEARCH_CACHE_TTL_SECONDS
        ]
        for key in expired_keys:
            del self._cache[key]

    def _get_cache_key(self, thread_id: str, query: str, **kwargs) -> str:
        """Generate a cache key for the query."""
        return f"{thread_id}:{query}:{kwargs}"

    def _extract_entities(self, query: str) -> Dict[str, List[str]]:
        """Extract entities (function names, class names, file names) from query.

        Args:
            query: Search query string

        Returns:
            Dict with functions, classes, files lists
        """
        entities = {
            "functions": [],
            "classes": [],
            "files": [],
        }

        camel_case_pattern = r"\b([A-Z][a-zA-Z0-9]+)\b"
        snake_case_pattern = r"\b([a-z_][a-z0-9_]*)\b"
        file_pattern = r"\b([\w/\.-]+\.(py|js|ts|tsx|html|css))\b"

        camel_matches = re.findall(camel_case_pattern, query)
        snake_matches = re.findall(snake_case_pattern, query)
        file_matches = re.findall(file_pattern, query)

        for match in camel_matches:
            if len(match) >= 3:
                entities["classes"].append(match)

        for match in snake_matches:
            if len(match) >= 3 and match not in [
                "the",
                "and",
                "for",
                "are",
                "but",
                "not",
                "all",
                "can",
                "has",
                "had",
                "may",
                "with",
                "this",
                "will",
                "your",
                "from",
                "they",
                "what",
                "so",
                "up",
                "out",
                "if",
                "about",
                "who",
                "get",
                "which",
                "go",
                "me",
            ]:
                entities["functions"].append(match)

        for match in file_matches:
            entities["files"].append(match[0])

        return entities

    def _keyword_search(
        self,
        workspace_path: str,
        thread_id: str,
        query: str,
        entities: Dict[str, List[str]],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Perform keyword-based search on indexed files.

        Args:
            workspace_path: Absolute path to workspace
            thread_id: Thread ID for isolation
            query: Search query
            entities: Extracted entities
            limit: Maximum results

        Returns:
            List of keyword search results with scores
        """
        results: List[Dict[str, Any]] = []
        wp = Path(workspace_path)

        all_keywords = set(query.lower().split())
        for _entity_type, entity_list in entities.items():
            for entity in entity_list:
                all_keywords.add(entity.lower())

        for file_path in collect_workspace_files(wp, extensions={"py"}):
            rel_path = str(file_path.relative_to(wp))

            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            keyword_count = 0
            for keyword in all_keywords:
                if keyword in content.lower():
                    keyword_count += 1

            if keyword_count == 0:
                continue

            keyword_score = min(keyword_count / len(all_keywords), 1.0)

            results.append(
                {
                    "file": rel_path,
                    "content": content[:500] + "..." if len(content) > 500 else content,
                    "keyword_score": keyword_score,
                    "type": "code",
                    "source": "keyword",
                }
            )

            if len(results) >= limit:
                return results

        return results

    def _get_graph_context(
        self,
        thread_id: str,
        entities: Dict[str, List[str]],
        max_depth: int = 2,
    ) -> Tuple[Dict[str, Set[str]], Dict[str, Any]]:
        """Get graph context (dependency paths and impacted files) for entities.

        Args:
            thread_id: Thread ID for isolation
            entities: Extracted entities
            max_depth: Maximum traversal depth

        Returns:
            Tuple of (related_files, graph_summary)
        """
        related_files: Dict[str, Set[str]] = {}
        graph_summary: Dict[str, Any] = {"impact_paths": [], "dependency_chains": []}

        if not settings.ENABLE_KNOWLEDGE_GRAPH:
            return related_files, graph_summary

        all_entity_names = []
        for _entity_type, entity_list in entities.items():
            all_entity_names.extend(entity_list)

        for entity_name in all_entity_names[:5]:
            impact_paths = knowledge_graph_service.find_impact(thread_id, entity_name, max_depth=min(max_depth, 2))
            dependency_chains = knowledge_graph_service.get_dependency_chain(
                thread_id, entity_name, max_depth=min(max_depth, 2)
            )

            for path in impact_paths[:5]:
                for node in path["path"]:
                    file_path = node.get("file")
                    if file_path:
                        if entity_name not in related_files:
                            related_files[entity_name] = set()
                        related_files[entity_name].add(file_path)
                graph_summary["impact_paths"].append(path)

            for chain in dependency_chains[:5]:
                for node in chain["path"]:
                    file_path = node.get("file")
                    if file_path:
                        if entity_name not in related_files:
                            related_files[entity_name] = set()
                        related_files[entity_name].add(file_path)
                graph_summary["dependency_chains"].append(chain)

        return related_files, graph_summary

    def _generate_summary(
        self,
        thread_id: str,
        file_path: str,
        entity_name: str,
        graph_summary: Dict[str, Any],
    ) -> str:
        """Generate a human-readable summary for a file.

        Args:
            thread_id: Thread ID for isolation
            file_path: File path
            entity_name: Related entity name
            graph_summary: Graph summary data

        Returns:
            Summary string
        """
        summaries = []

        caller_count = 0
        callers = []
        for path in graph_summary.get("impact_paths", []):
            for node in path["path"]:
                if node.get("file") == file_path and node.get("relation") == "calls":
                    caller_name = node.get("name")
                    if caller_name and caller_name not in callers:
                        callers.append(caller_name)
                        caller_count += 1

        if caller_count > 0:
            summaries.append(f"被 {caller_count} 处调用: {', '.join(callers[:3])}")

        dependency_count = 0
        dependencies = []
        for chain in graph_summary.get("dependency_chains", []):
            for node in chain["path"]:
                if node.get("file") == file_path:
                    dep_name = node.get("name")
                    if dep_name and dep_name != entity_name:
                        dependencies.append(dep_name)
                        dependency_count += 1

        if dependency_count > 0:
            summaries.append(f"依赖 {', '.join(dependencies[:3])}")

        if summaries:
            return "; ".join(summaries)

        return ""

    def _fuse_results(
        self,
        vector_results: List[Dict[str, Any]],
        keyword_results: List[Dict[str, Any]],
        graph_summary: Dict[str, Any],
        entities: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        """Fuse results from multiple sources with weighted scoring.

        Scoring weights:
        - Vector score: 0.5
        - Graph relevance: 0.3
        - Keyword match: 0.2

        Args:
            vector_results: Results from vector search
            keyword_results: Results from keyword search
            graph_summary: Graph context data
            entities: Extracted entities

        Returns:
            Fused and sorted results
        """
        fused: Dict[str, Dict[str, Any]] = {}

        all_entity_names = []
        for _entity_type, entity_list in entities.items():
            all_entity_names.extend(entity_list)

        def get_graph_score(file_path: str) -> float:
            if not settings.ENABLE_KNOWLEDGE_GRAPH:
                return 0.0

            score = 0.0
            match_count = 0

            for path in graph_summary.get("impact_paths", []):
                for node in path["path"]:
                    if node.get("file") == file_path:
                        match_count += 1

            for chain in graph_summary.get("dependency_chains", []):
                for node in chain["path"]:
                    if node.get("file") == file_path:
                        match_count += 1

            if match_count > 0:
                score = min(match_count / 5, 1.0)

            return score

        for result in vector_results:
            file_key = result.get("file", "")
            if file_key not in fused:
                fused[file_key] = {
                    "file": result.get("file", ""),
                    "content": result.get("content", ""),
                    "vector_score": result.get("score", 0.0),
                    "keyword_score": 0.0,
                    "graph_score": 0.0,
                    "source": [],
                    "metadata": result.get("metadata", {}),
                }
            fused[file_key]["source"].append("vector")
            if result.get("score", 0.0) > fused[file_key]["vector_score"]:
                fused[file_key]["vector_score"] = result.get("score", 0.0)

        for result in keyword_results:
            file_key = result.get("file", "")
            if file_key not in fused:
                fused[file_key] = {
                    "file": result.get("file", ""),
                    "content": result.get("content", ""),
                    "vector_score": 0.0,
                    "keyword_score": 0.0,
                    "graph_score": 0.0,
                    "source": [],
                    "metadata": {},
                }
            fused[file_key]["source"].append("keyword")
            if result.get("keyword_score", 0.0) > fused[file_key]["keyword_score"]:
                fused[file_key]["keyword_score"] = result.get("keyword_score", 0.0)

        for file_key in fused:
            fused[file_key]["graph_score"] = get_graph_score(file_key)

            vector_w = 0.5
            graph_w = 0.3
            keyword_w = 0.2

            final_score = (
                fused[file_key]["vector_score"] * vector_w
                + fused[file_key]["graph_score"] * graph_w
                + fused[file_key]["keyword_score"] * keyword_w
            )
            fused[file_key]["score"] = final_score

            if all_entity_names:
                first_entity = all_entity_names[0]
                summary = self._generate_summary("", file_key, first_entity, graph_summary)
                fused[file_key]["summary"] = summary

        sorted_results = sorted(
            fused.values(),
            key=lambda x: x["score"],
            reverse=True,
        )

        return sorted_results[: settings.SMART_SEARCH_MAX_RESULTS]

    async def smart_search(
        self,
        thread_id: str,
        query: str,
        workspace_path: str,
        limit: int = 10,
        file_pattern: Optional[str] = None,
        filter_function: Optional[str] = None,
        filter_class: Optional[str] = None,
        max_depth: int = 2,
    ) -> List[Dict[str, Any]]:
        """Perform smart hybrid search combining multiple retrieval strategies.

        Args:
            thread_id: Thread ID for workspace isolation
            query: Semantic search query
            workspace_path: Absolute path to workspace
            limit: Maximum number of results (default 10)
            file_pattern: Optional glob pattern to filter by file path
            filter_function: Optional function name to filter results
            filter_class: Optional class name to filter results
            max_depth: Maximum graph traversal depth (default 2)

        Returns:
            List of search result dicts with enhanced context
        """
        if not settings.ENABLE_SMART_SEARCH:
            return await code_index_service.search(thread_id, query, limit=limit, file_pattern=file_pattern)

        self._clean_cache()
        cache_key = self._get_cache_key(thread_id, query, limit=limit, file_pattern=file_pattern)
        if cache_key in self._cache:
            logger.debug(
                "smart_search_cache_hit",
                thread_id=thread_id,
                query=query,
            )
            return self._cache[cache_key]["results"]

        try:
            entities = self._extract_entities(query)
            logger.debug(
                "smart_search_entities_extracted",
                thread_id=thread_id,
                query=query,
                entities=entities,
            )

            vector_results = await code_index_service.search(
                thread_id,
                query,
                limit=max(limit * 2, 20),
                file_pattern=file_pattern,
                filter_function=filter_function,
                filter_class=filter_class,
            )

            keyword_results = self._keyword_search(
                workspace_path,
                thread_id,
                query,
                entities,
                limit=max(limit * 2, 20),
            )

            _, graph_summary = self._get_graph_context(thread_id, entities, max_depth=max_depth)

            fused_results = self._fuse_results(vector_results, keyword_results, graph_summary, entities)

            if file_pattern:
                fused_results = [r for r in fused_results if fnmatch.fnmatch(r.get("file", ""), file_pattern)]

            if filter_function:
                fused_results = [
                    r
                    for r in fused_results
                    if not filter_function
                    or filter_function.lower() in (r.get("metadata", {}).get("function", "").lower() or "")
                ]

            if filter_class:
                fused_results = [
                    r
                    for r in fused_results
                    if not filter_class
                    or filter_class.lower() in (r.get("metadata", {}).get("class", "").lower() or "")
                ]

            filtered_results = fused_results[:limit]

            self._cache[cache_key] = {
                "results": filtered_results,
                "timestamp": time.time(),
            }

            logger.info(
                "smart_search_completed",
                thread_id=thread_id,
                query=query,
                result_count=len(filtered_results),
                vector_count=len(vector_results),
                keyword_count=len(keyword_results),
            )

            return filtered_results

        except Exception as e:
            logger.error("smart_search_failed", thread_id=thread_id, error=str(e))
            return await code_index_service.search(thread_id, query, limit=limit, file_pattern=file_pattern)


smart_search_service = SmartSearchService()
