"""Semantic cache using pgvector embedding similarity.

Replaces the exact-match (SHA256 hash) cache with embedding-based cosine
similarity search.  Semantically equivalent queries ("如何实现登录" vs
"怎么实现登录功能") now hit the same cache entry.

Architecture
------------

1. **Embedding generation** — uses the configured embedding model
   (default: Bailian/DashScope ``text-embedding-v3``, 1024 dims) via the
   OpenAI-compatible API. Dimension is controlled by
   ``settings.LONG_TERM_MEMORY_EMBEDDING_DIM``.
2. **Storage** — a ``semantic_cache`` PostgreSQL table with a ``vector(N)``
   column (N = ``settings.LONG_TERM_MEMORY_EMBEDDING_DIM``), backed by the
   shared async connection pool (:mod:`app.core.db.pool`).
3. **Lookup** — cosine similarity search via pgvector's ``<=>`` operator.
   Entries above the threshold are cache hits; below are misses.
4. **Invalidation** — ``invalidate_user()`` deletes all entries for a given
   user + cache type (e.g. when new memories are added).

Two cache types are supported:

- ``"memory"`` — caches mem0 search results per user (threshold 0.90).
- ``"llm"`` — caches LLM text responses per user (threshold 0.95, stricter
  to avoid returning a wrong answer to a semantically different question).

All operations are **fail-safe**: cache errors are logged and swallowed so
they never break the main request flow.
"""

from typing import Awaitable, Callable, Optional

from openai import AsyncOpenAI
from psycopg import sql

from app.core.config import settings
from app.core.db.pool import get_pool, init_pool
from app.core.logging import logger

# SQL to create the semantic_cache table and indexes (idempotent).
# Vector dimension is injected at runtime from settings — must match the
# embedder's embedding_dims. Hardcoding 1024 would break if the user
# switches to a different embedding model (e.g. OpenAI text-embedding-3-small
# with 1536 dims).
_INIT_SQL_TEMPLATE = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS semantic_cache (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_type      TEXT NOT NULL,
    user_id         TEXT,
    query_text      TEXT NOT NULL,
    query_embedding vector(%d) NOT NULL,
    response_text   TEXT NOT NULL,
    model_name      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS semantic_cache_type_user_idx
    ON semantic_cache (cache_type, user_id, expires_at);

CREATE INDEX IF NOT EXISTS semantic_cache_embedding_idx
    ON semantic_cache USING hnsw (query_embedding vector_cosine_ops);
"""


def _embedding_to_str(embedding: list[float]) -> str:
    """Convert a float list to pgvector string literal ``[0.1,0.2,...]``."""
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


class SemanticCache:
    """pgvector-backed semantic cache with embedding similarity search.

    All methods are async and fail-safe — cache errors are logged and
    swallowed so they never propagate to the caller.
    """

    def __init__(self) -> None:
        """Initialize the semantic cache with lazy embedding client + table."""
        self._embedding_client: Optional[AsyncOpenAI] = None
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Create the DB table and index on first use (idempotent)."""
        if self._initialized:
            return

        try:
            pool = await init_pool()
            if pool is None:
                logger.warning("semantic_cache_init_skipped_no_pool")
                return

            async with pool.connection() as conn:
                init_sql = _INIT_SQL_TEMPLATE % settings.LONG_TERM_MEMORY_EMBEDDING_DIM
                await conn.execute(sql.SQL(init_sql))  # type: ignore[reportArgumentType]

            self._initialized = True
            logger.info("semantic_cache_initialized", embedding_dim=settings.LONG_TERM_MEMORY_EMBEDDING_DIM)
        except Exception as e:
            logger.error("semantic_cache_init_failed", error=str(e))

    def _get_embedding_client(self) -> AsyncOpenAI:
        """Lazily create the async OpenAI client for embeddings."""
        if self._embedding_client is None:
            self._embedding_client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,
                timeout=10,
            )
        return self._embedding_client

    async def _embed(self, text: str) -> Optional[list[float]]:
        """Generate an embedding vector for the given text.

        Returns ``None`` on failure (fail-safe).
        """
        try:
            client = self._get_embedding_client()
            response = await client.embeddings.create(
                model=settings.SEMANTIC_CACHE_EMBEDDING_MODEL,
                input=text[:8000],  # truncate to avoid token limits
                # Must match the vector(N) column in semantic_cache table.
                # Without this, the API returns the model's default dimension
                # (1536 for OpenAI, 1024 for text-embedding-v3), which may
                # not match the table schema.
                dimensions=settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
            )
            return response.data[0].embedding
        except Exception as e:
            logger.warning("semantic_cache_embed_failed", error=str(e))
            return None

    async def get(
        self,
        cache_type: str,
        user_id: Optional[str],
        query: str,
        threshold: float,
    ) -> Optional[str]:
        """Search for a semantically similar cached response.

        Args:
            cache_type: Cache scope (``"memory"`` or ``"llm"``).
            user_id: User identifier for per-user scoping.  ``None`` for
                global cache entries.
            query: The query text to search for.
            threshold: Minimum cosine similarity (0.0–1.0) for a hit.

        Returns:
            Cached response text if a hit is found, ``None`` on miss or
            failure.
        """
        if not settings.SEMANTIC_CACHE_ENABLED:
            return None

        await self._ensure_initialized()

        embedding = await self._embed(query)
        if embedding is None:
            return None

        embedding_str = _embedding_to_str(embedding)

        try:
            pool = get_pool()
            if pool is None:
                return None

            async with pool.connection() as conn:
                cursor = await conn.execute(
                    sql.SQL("""
                    SELECT response_text,
                           1 - (query_embedding <=> %s::vector) AS similarity
                    FROM semantic_cache
                    WHERE cache_type = %s
                      AND user_id IS NOT DISTINCT FROM %s
                      AND expires_at > now()
                    ORDER BY query_embedding <=> %s::vector
                    LIMIT 1
                    """),
                    (embedding_str, cache_type, user_id, embedding_str),
                )
                row = await cursor.fetchone()

            if row is None:
                return None

            response_text = row["response_text"]
            similarity = float(row["similarity"])

            if similarity >= threshold:
                logger.info(
                    "semantic_cache_hit",
                    cache_type=cache_type,
                    similarity=round(similarity, 4),
                    threshold=threshold,
                )
                return response_text

            logger.debug(
                "semantic_cache_below_threshold",
                cache_type=cache_type,
                similarity=round(similarity, 4),
                threshold=threshold,
            )
            return None
        except Exception as e:
            logger.warning("semantic_cache_get_failed", cache_type=cache_type, error=str(e))
            return None

    async def set(
        self,
        cache_type: str,
        user_id: Optional[str],
        query: str,
        response: str,
        ttl: Optional[int] = None,
        model_name: Optional[str] = None,
    ) -> None:
        """Store a query-response pair in the semantic cache.

        Args:
            cache_type: Cache scope (``"memory"`` or ``"llm"``).
            user_id: User identifier for per-user scoping.
            query: The query text.
            response: The response text to cache.
            ttl: Time-to-live in seconds.  Uses ``SEMANTIC_CACHE_TTL_SECONDS``
                if not specified.
            model_name: Optional LLM model name that generated the response.
        """
        if not settings.SEMANTIC_CACHE_ENABLED:
            return

        await self._ensure_initialized()

        embedding = await self._embed(query)
        if embedding is None:
            return

        embedding_str = _embedding_to_str(embedding)
        effective_ttl = ttl or settings.SEMANTIC_CACHE_TTL_SECONDS

        try:
            pool = get_pool()
            if pool is None:
                return

            async with pool.connection() as conn:
                await conn.execute(
                    sql.SQL("""
                    INSERT INTO semantic_cache
                        (cache_type, user_id, query_text, query_embedding,
                         response_text, model_name, expires_at)
                    VALUES (%s, %s, %s, %s::vector, %s, %s,
                            now() + make_interval(secs => %s))
                    """),
                    (cache_type, user_id, query[:8000], embedding_str, response, model_name, effective_ttl),
                )

            logger.debug(
                "semantic_cache_stored",
                cache_type=cache_type,
                ttl=effective_ttl,
            )
        except Exception as e:
            logger.warning("semantic_cache_set_failed", cache_type=cache_type, error=str(e))

    async def get_or_set(
        self,
        cache_type: str,
        user_id: Optional[str],
        query: str,
        producer: Callable[[], Awaitable[str]],
        threshold: Optional[float] = None,
        ttl: Optional[int] = None,
    ) -> str:
        """Check cache; on miss, call producer, store result, return.

        Convenience method that handles the full get-miss-set cycle.
        The embedding is generated once (for the lookup) and reused for
        storage only if needed — but since ``get`` and ``set`` each
        generate their own embedding, there's a minor redundancy.  For
        high-volume paths, call ``get`` and ``set`` directly and reuse
        the embedding.

        Args:
            cache_type: Cache scope.
            user_id: User identifier.
            query: Query text.
            producer: Async callable that produces the response on miss.
            threshold: Similarity threshold (uses type-specific default).
            ttl: TTL in seconds.

        Returns:
            Cached or freshly produced response text.
        """
        if threshold is None:
            threshold = (
                settings.SEMANTIC_CACHE_MEMORY_THRESHOLD
                if cache_type == "memory"
                else settings.SEMANTIC_CACHE_LLM_THRESHOLD
            )

        cached = await self.get(cache_type, user_id, query, threshold)
        if cached is not None:
            return cached

        response = await producer()
        if response:
            await self.set(cache_type, user_id, query, response, ttl=ttl)
        return response

    async def invalidate_user(self, cache_type: str, user_id: Optional[str]) -> None:
        """Delete all cache entries for a user + cache type.

        Called when underlying data changes (e.g. ``MemoryService.add()``
        invalidates the memory cache so stale results aren't returned).

        Args:
            cache_type: Cache scope to invalidate.
            user_id: User whose entries should be deleted.
        """
        if not settings.SEMANTIC_CACHE_ENABLED:
            return

        try:
            pool = get_pool()
            if pool is None:
                return

            async with pool.connection() as conn:
                await conn.execute(
                    sql.SQL("""
                    DELETE FROM semantic_cache
                    WHERE cache_type = %s AND user_id IS NOT DISTINCT FROM %s
                    """),
                    (cache_type, user_id),
                )

            logger.info(
                "semantic_cache_invalidated",
                cache_type=cache_type,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("semantic_cache_invalidate_failed", cache_type=cache_type, error=str(e))


# Module-level singleton.
semantic_cache: SemanticCache = SemanticCache()
