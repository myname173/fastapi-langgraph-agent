"""Code Index Service for semantic code search using embeddings and pgvector.

Uses mem0 AsyncMemory as the embedding + vector store layer, with a
dedicated collection for code snippets.
"""

import fnmatch
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any

from mem0 import AsyncMemory

from app.core.config import settings
from app.core.logging import logger
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.code_parser import collect_workspace_files, extract_module_structure, safe_ast_parse

# M-16 fix: shared circuit breaker for the code-index pgvector backend.
# Same rationale as memory_service — after 5 consecutive failures the
# circuit opens and short-circuits search() for 60s.
_code_index_breaker = CircuitBreaker(
    name="code_index_pgvector",
    failure_threshold=5,
    cooldown_seconds=60.0,
)


class CodeIndexService:
    """Service for building and querying a semantic code index.

    The index stores chunked code snippets as embeddings in pgvector
    (via mem0), keyed by a synthetic user_id that encodes the workspace
    thread so each workspace has its own isolated index.
    """

    def __init__(self):
        """Initialize the code index service."""
        self._memory: Optional[AsyncMemory] = None
        self._file_hashes: dict[str, str] = {}

    async def _get_memory(self) -> AsyncMemory:
        if self._memory is None:
            self._memory = await AsyncMemory.from_config(
                config_dict={
                    "vector_store": {
                        "provider": "pgvector",
                        "config": {
                            "collection_name": settings.CODE_INDEX_COLLECTION_NAME,
                            "dbname": settings.POSTGRES_DB,
                            "user": settings.POSTGRES_USER,
                            "password": settings.POSTGRES_PASSWORD,
                            "host": settings.POSTGRES_HOST,
                            "port": settings.POSTGRES_PORT,
                            # Must match embedder's embedding_dims — see
                            # memory.py for the same fix. PGVectorConfig
                            # defaults to 1536, but qwen3.7-text-embedding uses 1024.
                            "embedding_model_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
                        },
                    },
                    "llm": {
                        "provider": "openai",
                        "config": {
                            "model": settings.LONG_TERM_MEMORY_MODEL,
                            # Explicit key + base_url: mem0 otherwise falls back
                            # to os.getenv("OPENAI_API_KEY"), which stays empty
                            # because this project injects keys via Docker
                            # secrets. Same rationale as memory.py.
                            "api_key": settings.CHAT_API_KEY,
                            "openai_base_url": settings.CHAT_BASE_URL,
                        },
                    },
                    "embedder": {
                        "provider": "openai",
                        "config": {
                            "model": settings.LONG_TERM_MEMORY_EMBEDDER_MODEL,
                            # Required for Bailian qwen3.7-text-embedding (1024 dims).
                            # Without embedding_dims, mem0 defaults to 1536
                            # and the embedding API rejects with 400.
                            "embedding_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
                            # Code chunk vectors → EMBEDDING provider.
                            "api_key": settings.EMBEDDING_API_KEY,
                            "openai_base_url": settings.EMBEDDING_BASE_URL,
                        },
                    },
                }
            )
        return self._memory

    def _file_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _parse_python_structure(self, content: str) -> Dict[str, Any]:
        """Parse Python file to extract structural information.

        Delegates to app.utils.code_parser (L-12 consolidation). The original
        implementation had an O(n^2) nested ast.walk for parent-class lookup;
        the shared extract_module_structure builds a single-pass lookup map.

        Args:
            content: Python source code

        Returns:
            Dict with functions, classes, imports, and line ranges
        """
        if not settings.CODE_INDEX_STRUCTURAL_PARSING:
            return {"functions": [], "classes": [], "imports": [], "success": False}

        tree = safe_ast_parse(content)
        if tree is None:
            return {"functions": [], "classes": [], "imports": [], "success": False}

        return extract_module_structure(tree)

    def _chunk_code(self, content: str, file_path: str) -> List[str]:
        """Split code content into overlapping chunks by lines.

        Args:
            content: File content
            file_path: Path to the file (used in chunk headers)

        Returns:
            List of chunk strings
        """
        lines = content.splitlines()
        chunk_size = settings.CODE_INDEX_CHUNK_SIZE
        overlap = settings.CODE_INDEX_CHUNK_OVERLAP

        if len(lines) <= chunk_size:
            return [f"File: {file_path}\n{content}"]

        chunks = []
        start = 0
        while start < len(lines):
            end = min(start + chunk_size, len(lines))
            chunk_lines = lines[start:end]
            chunk = f"File: {file_path} (lines {start + 1}-{end})\n" + "\n".join(chunk_lines)
            chunks.append(chunk)
            start += chunk_size - overlap
            if start >= len(lines):
                break

        return chunks

    def _create_structured_chunks(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Create structured chunks with metadata.

        For Python files with structural parsing enabled, creates function-level chunks.
        For other files, falls back to line-based chunking.

        Args:
            content: File content
            file_path: Path to the file

        Returns:
            List of dicts with content and metadata for each chunk
        """
        lines = content.splitlines()
        language = file_path.split(".")[-1] if "." in file_path else "unknown"
        structure = self._parse_python_structure(content) if language == "py" else {}

        if structure.get("success") and structure.get("functions"):
            chunks = []
            processed_lines: set[int] = set()

            for func in structure["functions"]:
                start_line = func["start_line"] - 1
                end_line = func["end_line"]
                func_lines = lines[start_line:end_line]
                func_name = func["name"]
                class_name = func.get("class", "")

                header_parts = [f"File: {file_path}"]
                if class_name:
                    header_parts.append(f"Class: {class_name}")
                header_parts.append(f"Function: {func_name}")
                header = " | ".join(header_parts)

                chunk_content = f"{header}\n" + "\n".join(func_lines)
                chunk_metadata = {
                    "file": file_path,
                    "language": language,
                    "chunk_type": "function",
                    "function": func_name,
                    "class": class_name,
                    "start_line": start_line + 1,
                    "end_line": end_line,
                }

                chunks.append(
                    {
                        "content": chunk_content,
                        "metadata": chunk_metadata,
                    }
                )

                for i in range(start_line, end_line):
                    processed_lines.add(i)

            for cls in structure["classes"]:
                class_lines = set(range(cls["start_line"] - 1, cls["end_line"]))
                if class_lines.isdisjoint(processed_lines):
                    cls_lines = lines[cls["start_line"] - 1 : cls["end_line"]]
                    header = f"File: {file_path} | Class: {cls['name']}"
                    chunk_content = f"{header}\n" + "\n".join(cls_lines)
                    chunks.append(
                        {
                            "content": chunk_content,
                            "metadata": {
                                "file": file_path,
                                "language": language,
                                "chunk_type": "class",
                                "class": cls["name"],
                                "start_line": cls["start_line"],
                                "end_line": cls["end_line"],
                            },
                        }
                    )

            unprocessed_start = 0
            chunk_size = settings.CODE_INDEX_CHUNK_SIZE
            while unprocessed_start < len(lines):
                unprocessed_end = min(unprocessed_start + chunk_size, len(lines))
                has_unprocessed = False
                for i in range(unprocessed_start, unprocessed_end):
                    if i not in processed_lines:
                        has_unprocessed = True
                        break

                if has_unprocessed:
                    chunk_lines = lines[unprocessed_start:unprocessed_end]
                    header = f"File: {file_path} (lines {unprocessed_start + 1}-{unprocessed_end})"
                    chunk_content = f"{header}\n" + "\n".join(chunk_lines)
                    chunks.append(
                        {
                            "content": chunk_content,
                            "metadata": {
                                "file": file_path,
                                "language": language,
                                "chunk_type": "partial",
                                "start_line": unprocessed_start + 1,
                                "end_line": unprocessed_end,
                            },
                        }
                    )

                unprocessed_start += chunk_size - settings.CODE_INDEX_CHUNK_OVERLAP

            return chunks
        else:
            text_chunks = self._chunk_code(content, file_path)
            return [
                {
                    "content": chunk,
                    "metadata": {
                        "file": file_path,
                        "language": language,
                        "chunk_type": "text",
                    },
                }
                for chunk in text_chunks
            ]

    def _collect_files(self, workspace_path: Path) -> List[Path]:
        """Collect all indexable files in a workspace.

        Delegates to app.utils.code_parser.collect_workspace_files (L-12
        consolidation). The original duplicated the hidden-dir + __pycache__
        filtering that the shared utility now centralizes.
        """
        return collect_workspace_files(
            workspace_path,
            extensions=set(settings.SDLC_ALLOWED_EXTENSIONS),
        )

    async def build_index(self, workspace_path: str, thread_id: str) -> dict:
        """Build or rebuild the code index for a workspace.

        Args:
            workspace_path: Absolute path to the workspace directory
            thread_id: Thread ID used as the mem0 user_id for isolation

        Returns:
            Stats dict with file_count, chunk_count, skipped_count
        """
        if not settings.CODE_INDEX_ENABLED:
            return {"file_count": 0, "chunk_count": 0, "skipped": 0, "enabled": False}

        wp = Path(workspace_path)
        if not wp.is_dir():
            raise ValueError(f"Workspace path is not a directory: {workspace_path}")

        memory = await self._get_memory()
        files = self._collect_files(wp)

        indexed_files = 0
        total_chunks = 0
        skipped = 0

        for file_path in files:
            try:
                rel_path = str(file_path.relative_to(wp))
                content = file_path.read_text(encoding="utf-8")
                content_hash = self._file_hash(content)

                if len(content.encode("utf-8")) > settings.CODE_INDEX_MAX_FILE_SIZE:
                    skipped += 1
                    continue

                cache_key = f"{thread_id}:{rel_path}"
                if cache_key in self._file_hashes and self._file_hashes[cache_key] == content_hash:
                    skipped += 1
                    continue

                structured_chunks = self._create_structured_chunks(content, rel_path)
                for chunk_index, chunk_data in enumerate(structured_chunks):
                    merged_metadata = {
                        "file": rel_path,
                        "type": "code",
                        "chunk_index": chunk_index,
                        "hash": content_hash,
                        "last_modified": file_path.stat().st_mtime,
                    }
                    merged_metadata.update(chunk_data.get("metadata", {}))

                    await memory.add(
                        [{"role": "user", "content": chunk_data["content"]}],
                        user_id=thread_id,
                        metadata=merged_metadata,
                    )
                    total_chunks += 1

                self._file_hashes[cache_key] = content_hash
                indexed_files += 1

            except UnicodeDecodeError:
                skipped += 1
                continue
            except Exception as e:
                logger.warning(
                    "code_index_file_failed",
                    file=str(file_path),
                    error=str(e),
                )
                skipped += 1

        logger.info(
            "code_index_build_complete",
            thread_id=thread_id,
            files_indexed=indexed_files,
            total_chunks=total_chunks,
            skipped=skipped,
        )

        return {
            "file_count": indexed_files,
            "chunk_count": total_chunks,
            "skipped": skipped,
            "enabled": True,
        }

    async def update_file(self, workspace_path: str, thread_id: str, file_path: str) -> dict:
        """Incrementally update the index for a single file.

        Args:
            workspace_path: Absolute path to workspace
            thread_id: Thread ID for isolation
            file_path: Relative path of the file to update

        Returns:
            Stats dict with chunk_count and file_path
        """
        if not settings.CODE_INDEX_ENABLED:
            return {"chunk_count": 0, "enabled": False}

        wp = Path(workspace_path)
        abs_path = wp / file_path

        if not abs_path.is_file():
            raise ValueError(f"File not found: {file_path}")

        content = abs_path.read_text(encoding="utf-8")

        if len(content.encode("utf-8")) > settings.CODE_INDEX_MAX_FILE_SIZE:
            return {"chunk_count": 0, "file_path": file_path, "skipped": True, "reason": "file_too_large"}

        content_hash = self._file_hash(content)
        memory = await self._get_memory()
        structured_chunks = self._create_structured_chunks(content, file_path)

        for chunk_index, chunk_data in enumerate(structured_chunks):
            merged_metadata = {
                "file": file_path,
                "type": "code",
                "chunk_index": chunk_index,
                "hash": content_hash,
                "last_modified": abs_path.stat().st_mtime,
            }
            merged_metadata.update(chunk_data.get("metadata", {}))

            await memory.add(
                [{"role": "user", "content": chunk_data["content"]}],
                user_id=thread_id,
                metadata=merged_metadata,
            )

        cache_key = f"{thread_id}:{file_path}"
        self._file_hashes[cache_key] = content_hash

        logger.info(
            "code_index_file_updated",
            thread_id=thread_id,
            file=file_path,
            chunk_count=len(structured_chunks),
        )

        return {"chunk_count": len(structured_chunks), "file_path": file_path}

    async def search(
        self,
        thread_id: str,
        query: str,
        limit: int = 10,
        file_pattern: Optional[str] = None,
        filter_function: Optional[str] = None,
        filter_class: Optional[str] = None,
    ) -> List[dict]:
        """Search the code index for relevant code snippets.

        Args:
            thread_id: Thread ID for workspace isolation
            query: Semantic search query
            limit: Maximum number of results to return
            file_pattern: Optional glob pattern to filter by file path
                (e.g. "*.py", "app/core/**/*.py")
            filter_function: Optional function name to filter results
            filter_class: Optional class name to filter results

        Returns:
            List of dicts with file, content, score, and full metadata
        """
        if not settings.CODE_INDEX_ENABLED:
            return []

        # M-16 fix: short-circuit when pgvector is persistently down.
        if _code_index_breaker.is_open:
            logger.debug("code_index_search_short_circuited", thread_id=thread_id)
            return []

        try:
            memory = await self._get_memory()
            results = await memory.search(user_id=thread_id, query=query)

            hits = results.get("results", []) if isinstance(results, dict) else []
            formatted: List[dict] = []

            for hit in hits:
                metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
                file_path = metadata.get("file", "unknown")

                if file_pattern and not fnmatch.fnmatch(file_path, file_pattern):
                    continue

                if filter_function:
                    chunk_func = metadata.get("function", "")
                    if chunk_func and filter_function.lower() not in chunk_func.lower():
                        continue

                if filter_class:
                    chunk_class = metadata.get("class", "")
                    if chunk_class and filter_class.lower() not in chunk_class.lower():
                        continue

                formatted.append(
                    {
                        "file": file_path,
                        "content": hit.get("memory", ""),
                        "score": hit.get("score", 0.0),
                        "type": metadata.get("type", "code"),
                        "metadata": metadata,
                    }
                )

                if len(formatted) >= limit:
                    break

            logger.debug(
                "code_index_search",
                thread_id=thread_id,
                result_count=len(formatted),
                file_pattern=file_pattern or "none",
                filter_function=filter_function or "none",
                filter_class=filter_class or "none",
            )

            _code_index_breaker.record_success()
            return formatted

        except Exception as e:
            _code_index_breaker.record_failure()
            logger.error("code_index_search_failed", error=str(e), thread_id=thread_id)
            return []


code_index_service = CodeIndexService()
