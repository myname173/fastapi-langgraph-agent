"""Long-term memory service using mem0 and pgvector with semantic cache layer."""

from mem0 import AsyncMemory

from app.core.config import settings
from app.core.logging import logger
from app.core.semantic_cache import semantic_cache
from app.utils.circuit_breaker import CircuitBreaker

# M-16 fix: circuit breaker for the mem0/pgvector backend. After 5
# consecutive failures the circuit opens and short-circuits search()/add()
# for 60s, avoiding repeated retry storms (and pgvector connection spam)
# when the backend is persistently down. After cooldown, one probe call
# is allowed; success closes the circuit, failure re-opens it.
_memory_breaker = CircuitBreaker(
    name="mem0_pgvector",
    failure_threshold=5,
    cooldown_seconds=60.0,
)


class MemoryService:
    """Service for managing long-term memory using mem0 and pgvector."""

    def __init__(self):
        """Initialize the memory service."""
        self._memory: AsyncMemory | None = None

    async def _get_memory(self) -> AsyncMemory:
        if self._memory is None:
            self._memory = await AsyncMemory.from_config(
                config_dict={
                    "vector_store": {
                        "provider": "pgvector",
                        "config": {
                            "collection_name": settings.LONG_TERM_MEMORY_COLLECTION_NAME,
                            "dbname": settings.POSTGRES_DB,
                            "user": settings.POSTGRES_USER,
                            "password": settings.POSTGRES_PASSWORD,
                            "host": settings.POSTGRES_HOST,
                            "port": settings.POSTGRES_PORT,
                            # Critical: must match embedder's embedding_dims.
                            # PGVectorConfig defaults to 1536 (OpenAI
                            # text-embedding-3-small), but Bailian
                            # text-embedding-v3 uses 1024. Without this,
                            # the table is created as vector(1536) while
                            # the embedder returns 1024-dim vectors, causing
                            # "expected 1536 dimensions, not 1024" on INSERT.
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
                            # Critical for Bailian text-embedding-v3: the model
                            # only supports dims in {64,128,256,512,768,1024}.
                            # Without embedding_dims, mem0 defaults to OpenAI's
                            # 1536, which causes "dimension mismatch" errors
                            # when inserting vectors into pgvector.
                            "embedding_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
                        },
                    },
                }
            )
        return self._memory

    async def initialize(self) -> None:
        """Pre-warm the mem0 AsyncMemory instance and its pgvector connection pool.

        Call once at startup so the first search() or add() doesn't pay the
        ~130ms from_config + pgvector.list_cols() cold-init cost.
        """
        await self._get_memory()
        logger.info("memory_service_initialized")

    async def search(self, user_id: str | None, query: str) -> str:
        """Search relevant memories for a user.

        Checks semantic cache first; on miss, queries mem0 and caches the
        result.  Semantic caching means semantically equivalent queries
        ("如何实现登录" vs "怎么实现登录功能") hit the same cache entry.

        Returns formatted memory string, or empty string on failure or when
        no user_id is supplied (anonymous sessions skip long-term memory
        rather than pooling under a shared partition).
        """
        if user_id is None:
            return ""
        # M-16 fix: short-circuit when pgvector is persistently down. This
        # check runs BEFORE the semantic cache so that an open circuit
        # returns "" without caching the empty result — caching "" would
        # mask backend recovery on subsequent calls.
        if _memory_breaker.is_open:
            logger.debug("memory_search_short_circuited", user_id=user_id)
            return ""
        try:

            async def _do_search() -> str:
                memory = await self._get_memory()
                results = await memory.search(user_id=str(user_id), query=query)
                return "\n".join([f"* {r['memory']}" for r in results["results"]])

            async def _guarded_search() -> str:
                # Record success/failure only on an actual mem0 call (cache
                # miss). A cache hit must NOT reset the failure count, or
                # intermittent hits would prevent the circuit from ever
                # opening during a sustained outage.
                try:
                    result = await _do_search()
                    _memory_breaker.record_success()
                    return result
                except Exception:
                    _memory_breaker.record_failure()
                    raise

            return await semantic_cache.get_or_set(
                cache_type="memory",
                user_id=str(user_id),
                query=query,
                producer=_guarded_search,
            )
        except Exception as e:
            logger.error("failed_to_get_relevant_memory", error=str(e), user_id=user_id, query=query)
            return ""

    async def add(self, user_id: str | None, messages: list[dict], metadata: dict | None = None) -> None:
        """Add messages to long-term memory for a user.

        Invalidates the user's semantic cache so subsequent searches reflect
        the new memories immediately.

        No-op when ``user_id`` is ``None`` (see ``search`` for rationale).
        """
        if user_id is None:
            return
        # M-16 fix: short-circuit when pgvector is persistently down.
        if _memory_breaker.is_open:
            logger.debug("memory_add_short_circuited", user_id=user_id)
            return
        try:
            memory = await self._get_memory()
            await memory.add(messages, user_id=str(user_id), metadata=metadata)
            _memory_breaker.record_success()
            await semantic_cache.invalidate_user("memory", str(user_id))
            logger.info("long_term_memory_updated_successfully", user_id=user_id)
        except Exception as e:
            _memory_breaker.record_failure()
            logger.exception("failed_to_update_long_term_memory", user_id=user_id, error=str(e))


memory_service = MemoryService()
