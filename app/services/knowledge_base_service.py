"""Knowledge Base Service for RAG over user-uploaded documents.

Uses mem0 AsyncMemory as the embedding + vector store layer, with a
dedicated collection (separate from code_index). Documents are isolated
per-user (user_id=str(user_id)), shared across all tasks.

Supported file types:
- .md, .txt, .json, .yaml, .yml — UTF-8 text (zero extra deps)
- .pdf — lazy import PyMuPDF (fitz); skipped with warning if not installed
"""

import hashlib
import uuid
from typing import Any, Dict, List, Optional

from mem0 import AsyncMemory

from app.core.config import settings
from app.core.logging import logger
from app.utils.circuit_breaker import CircuitBreaker

# M-16 fix: shared circuit breaker for the knowledge-base pgvector backend.
# After 5 consecutive failures the circuit opens and short-circuits search()
# for 60s, mirroring memory_service and code_index_service.
_kb_breaker = CircuitBreaker(
    name="kb_pgvector",
    failure_threshold=5,
    cooldown_seconds=60.0,
)


class KnowledgeBaseService:
    """Service for indexing and searching user-uploaded documents.

    Parallel to :class:`CodeIndexService` but uses paragraph-aware
    chunking (vs. AST-based structural chunking for code) and per-user
    isolation (vs. per-thread for code index).
    """

    def __init__(self) -> None:
        """Initialize the knowledge base service."""
        self._memory: Optional[AsyncMemory] = None

    async def _get_memory(self) -> AsyncMemory:
        if self._memory is None:
            self._memory = await AsyncMemory.from_config(
                config_dict={
                    "vector_store": {
                        "provider": "pgvector",
                        "config": {
                            "collection_name": settings.KNOWLEDGE_BASE_COLLECTION_NAME,
                            "dbname": settings.POSTGRES_DB,
                            "user": settings.POSTGRES_USER,
                            "password": settings.POSTGRES_PASSWORD,
                            "host": settings.POSTGRES_HOST,
                            "port": settings.POSTGRES_PORT,
                            # Must match embedder's embedding_dims — see
                            # memory.py for the same fix. PGVectorConfig
                            # defaults to 1536, but text-embedding-v3 uses 1024.
                            "embedding_model_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
                        },
                    },
                    "llm": {
                        "provider": "openai",
                        "config": {"model": settings.LONG_TERM_MEMORY_MODEL},
                    },
                    "embedder": {
                        "provider": "openai",
                        "config": {
                            "model": settings.LONG_TERM_MEMORY_EMBEDDER_MODEL,
                            "embedding_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
                        },
                    },
                }
            )
        return self._memory

    def _extract_text(self, content: bytes, filename: str) -> str:
        """Extract text content from a uploaded file.

        For text-based formats (.md, .txt, .json, .yaml, .yml), decodes
        as UTF-8. For PDF, uses PyMuPDF (lazy import).

        Args:
            content: Raw file bytes.
            filename: Original filename (used to detect file type).

        Returns:
            Extracted text string. Empty string if extraction fails.
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

        if ext == "pdf":
            return self._extract_pdf_text(content)

        return content.decode("utf-8", errors="replace")

    def _extract_pdf_text(self, content: bytes) -> str:
        """Extract text from PDF bytes using PyMuPDF (lazy import).

        PyMuPDF is NOT a hard dependency — if not installed, PDF uploads
        are skipped with a warning. Install via ``uv pip install pymupdf``.
        """
        try:
            import fitz  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "pymupdf_not_installed",
                reason="PDF parsing requires 'pymupdf' package. Install via: uv pip install pymupdf",
            )
            return ""

        try:
            doc = fitz.open(stream=content, filetype="pdf")
            pages: List[str] = []
            for page in doc:
                pages.append(page.get_text())
            doc.close()
            return "\n\n".join(pages)
        except Exception as e:
            logger.warning("pdf_extraction_failed", error=str(e))
            return ""

    def _chunk_document(self, content: str, filename: str, doc_id: str) -> List[Dict[str, Any]]:
        """Split document text into overlapping chunks by paragraphs.

        Uses a character-based sliding window that respects paragraph
        boundaries (double newlines). Small paragraphs are merged to
        reach ``KB_CHUNK_SIZE``; large paragraphs are split at the
        boundary.

        Args:
            content: Full document text.
            filename: Original filename for chunk headers.
            doc_id: Document UUID for metadata tracking.

        Returns:
            List of dicts with ``content`` and ``metadata`` keys.
        """
        if not content.strip():
            return []

        chunk_size = settings.KB_CHUNK_SIZE
        overlap = settings.KB_CHUNK_OVERLAP
        paragraphs = content.split("\n\n")
        chunks: List[Dict[str, Any]] = []
        current_text = ""
        chunk_index = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_text) + len(para) + 2 > chunk_size and current_text:
                header = f"Document: {filename}"
                chunks.append(
                    {
                        "content": f"{header}\n{current_text}",
                        "metadata": {
                            "doc_id": doc_id,
                            "filename": filename,
                            "type": "document",
                            "chunk_index": chunk_index,
                        },
                    }
                )
                chunk_index += 1
                tail = current_text[-overlap:] if overlap > 0 else ""
                current_text = tail + "\n\n" + para if tail else para
            else:
                current_text = current_text + "\n\n" + para if current_text else para

        if current_text.strip():
            header = f"Document: {filename}"
            chunks.append(
                {
                    "content": f"{header}\n{current_text}",
                    "metadata": {
                        "doc_id": doc_id,
                        "filename": filename,
                        "type": "document",
                        "chunk_index": chunk_index,
                    },
                }
            )

        return chunks

    def _content_hash(self, content: bytes) -> str:
        """Compute SHA256 hash of file content for deduplication."""
        return hashlib.sha256(content).hexdigest()

    async def upload_document(self, user_id: int, filename: str, content: bytes) -> dict:
        """Parse, chunk, and index a document for the user.

        Args:
            user_id: User ID for per-user isolation.
            filename: Original filename.
            content: Raw file bytes.

        Returns:
            Stats dict with doc_id, filename, chunk_count, and status.
        """
        if not settings.ENABLE_KNOWLEDGE_BASE:
            return {"enabled": False, "chunk_count": 0}

        content_hash = self._content_hash(content)
        doc_id = str(uuid.uuid4())
        user_id_str = str(user_id)

        text = self._extract_text(content, filename)
        if not text.strip():
            return {
                "doc_id": doc_id,
                "filename": filename,
                "chunk_count": 0,
                "status": "extraction_failed",
            }

        chunks = self._chunk_document(text, filename, doc_id)
        if not chunks:
            return {
                "doc_id": doc_id,
                "filename": filename,
                "chunk_count": 0,
                "status": "empty_content",
            }

        memory = await self._get_memory()
        for chunk_data in chunks:
            merged_metadata = {
                **chunk_data["metadata"],
                "hash": content_hash,
            }
            await memory.add(
                [{"role": "user", "content": chunk_data["content"]}],
                user_id=user_id_str,
                metadata=merged_metadata,
            )

        logger.info(
            "kb_document_uploaded",
            user_id=user_id_str,
            doc_id=doc_id,
            filename=filename,
            chunk_count=len(chunks),
        )

        return {
            "doc_id": doc_id,
            "filename": filename,
            "chunk_count": len(chunks),
            "content_hash": content_hash,
            "status": "indexed",
        }

    async def search(self, user_id: str, query: str, limit: Optional[int] = None) -> List[dict]:
        """Semantic search over the user's knowledge base.

        Args:
            user_id: User ID for per-user isolation.
            query: Semantic search query.
            limit: Maximum results (defaults to ``KB_MAX_RESULTS``).

        Returns:
            List of dicts with content, score, filename, and metadata.
        """
        if not settings.ENABLE_KNOWLEDGE_BASE:
            return []

        # M-16 fix: short-circuit when pgvector is persistently down.
        if _kb_breaker.is_open:
            logger.debug("kb_search_short_circuited", user_id=user_id)
            return []

        max_results = limit or settings.KB_MAX_RESULTS
        try:
            memory = await self._get_memory()
            results = await memory.search(user_id=user_id, query=query)
            hits = results.get("results", []) if isinstance(results, dict) else []

            formatted: List[dict] = []
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                metadata = hit.get("metadata", {})
                formatted.append(
                    {
                        "content": hit.get("memory", ""),
                        "score": hit.get("score", 0.0),
                        "filename": metadata.get("filename", "unknown"),
                        "doc_id": metadata.get("doc_id", ""),
                        "type": "document",
                        "metadata": metadata,
                    }
                )
                if len(formatted) >= max_results:
                    break

            logger.debug(
                "kb_search",
                user_id=user_id,
                result_count=len(formatted),
            )
            _kb_breaker.record_success()
            return formatted

        except Exception as e:
            _kb_breaker.record_failure()
            logger.error("kb_search_failed", user_id=user_id, error=str(e))
            return []

    async def delete_document(self, user_id: str, doc_id: str) -> dict:
        """Delete all chunks of a document from the user's knowledge base.

        Uses mem0's ``get_all`` to find chunks by doc_id metadata, then
        deletes each by memory_id.

        Args:
            user_id: User ID for per-user isolation.
            doc_id: Document UUID to delete.

        Returns:
            Stats dict with deleted_count and doc_id.
        """
        if not settings.ENABLE_KNOWLEDGE_BASE:
            return {"enabled": False, "deleted_count": 0}

        try:
            memory = await self._get_memory()
            all_memories = await memory.get_all(user_id=user_id)
            memories = all_memories.get("results", []) if isinstance(all_memories, dict) else []

            deleted_count = 0
            for mem in memories:
                if not isinstance(mem, dict):
                    continue
                metadata = mem.get("metadata", {})
                if metadata.get("doc_id") == doc_id:
                    mem_id = mem.get("id", "")
                    if mem_id:
                        await memory.delete(memory_id=mem_id)
                        deleted_count += 1

            logger.info(
                "kb_document_deleted",
                user_id=user_id,
                doc_id=doc_id,
                deleted_count=deleted_count,
            )
            return {"doc_id": doc_id, "deleted_count": deleted_count}

        except Exception as e:
            logger.error(
                "kb_delete_failed",
                user_id=user_id,
                doc_id=doc_id,
                error=str(e),
            )
            return {"doc_id": doc_id, "deleted_count": 0, "error": str(e)}

    async def list_documents(self, user_id: str) -> List[dict]:
        """List all documents in the user's knowledge base.

        Groups indexed chunks by doc_id and returns aggregate metadata.

        Args:
            user_id: User ID for per-user isolation.

        Returns:
            List of document dicts with doc_id, filename, chunk_count.
        """
        if not settings.ENABLE_KNOWLEDGE_BASE:
            return []

        try:
            memory = await self._get_memory()
            all_memories = await memory.get_all(user_id=user_id)
            memories = all_memories.get("results", []) if isinstance(all_memories, dict) else []

            docs: Dict[str, dict] = {}
            for mem in memories:
                if not isinstance(mem, dict):
                    continue
                metadata = mem.get("metadata", {})
                doc_id = metadata.get("doc_id", "")
                if not doc_id:
                    continue
                if doc_id not in docs:
                    docs[doc_id] = {
                        "doc_id": doc_id,
                        "filename": metadata.get("filename", "unknown"),
                        "chunk_count": 0,
                        "content_hash": metadata.get("hash", ""),
                    }
                docs[doc_id]["chunk_count"] += 1

            return list(docs.values())

        except Exception as e:
            logger.error("kb_list_failed", user_id=user_id, error=str(e))
            return []


knowledge_base_service = KnowledgeBaseService()
