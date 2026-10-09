"""Long-term memory service using mem0 and pgvector with semantic cache layer."""

import os

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

# Minimum cosine similarity for a memory to be considered relevant.
# Without this filter, mem0 returns ALL stored memories for the user
# regardless of relevance — e.g., when the user asks about "半导体" but
# the only stored memories are about "600519", those irrelevant memories
# get injected into the prompt and bias the LLM to respond about the
# wrong stock.
_MEMORY_RELEVANCE_THRESHOLD = 0.7

# When long-term memory (mem0/pgvector) is unavailable — e.g. in the
# offline SDLC eval harness where no Postgres is provisioned — set
# MEMORY_SERVICE_ENABLED=false to short-circuit every search()/add() call
# and avoid a 30s pgvector connection timeout per request. Production keeps
# this enabled (default).
_MEMORY_SERVICE_ENABLED = os.getenv("MEMORY_SERVICE_ENABLED", "true").lower() not in (
    "false",
    "0",
    "no",
)


def _filter_messages_for_memory(messages: list[dict]) -> list[dict]:
    """Drop noise roles so mem0 extracts memories from real dialogue only.

    Tool results (``role="tool"``) and system prompts are excluded — they
    carry raw market/news data and instructions, not user facts, and mem0's
    extraction LLM would otherwise distill them into misleading memories
    (e.g. extracting "用户关注贵州茅台" from a tool result that merely quoted
    茅台's price while answering a sector question about 半导体). Assistant
    turns that are pure ``tool_calls`` (no textual content) are also dropped.

    Args:
        messages: OpenAI-format messages to be persisted via mem0 ``add``.

    Returns:
        Filtered list keeping only ``user`` messages and ``assistant``
        messages that carry textual content.
    """
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        if role in ("system", "tool"):
            continue
        if role == "assistant":
            content = m.get("content")
            if not (isinstance(content, str) and content.strip()):
                # Assistant turn with only tool_calls / empty content.
                continue
        out.append(m)
    return out


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
                            # qwen3.7-text-embedding uses 1024. Without this,
                            # the table is created as vector(1536) while
                            # the embedder returns 1024-dim vectors, causing
                            # "expected 1536 dimensions, not 1024" on INSERT.
                            "embedding_model_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
                        },
                    },
                    "llm": {
                        "provider": "openai",
                        "config": {
                            "model": settings.LONG_TERM_MEMORY_MODEL,
                            # mem0 falls back to os.getenv("OPENAI_API_KEY"),
                            # but this project injects the key via Docker
                            # secrets (CHAT_API_KEY_FILE) — the process env
                            # var stays empty, so mem0 would 401. Pass the key
                            # and base_url explicitly from settings instead.
                            # This is the fact-extraction LLM → CHAT provider.
                            "api_key": settings.CHAT_API_KEY,
                            "openai_base_url": settings.CHAT_BASE_URL,
                        },
                    },
                    "embedder": {
                        "provider": "openai",
                        "config": {
                            "model": settings.LONG_TERM_MEMORY_EMBEDDER_MODEL,
                            # Critical for Bailian qwen3.7-text-embedding: the
                            # model only supports dims in
                            # {256,512,768,1024,1536,2048,2560}.
                            # Without embedding_dims, mem0 defaults to OpenAI's
                            # 1536, which causes "dimension mismatch" errors
                            # when inserting vectors into pgvector.
                            "embedding_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIM,
                            # Same Docker-secrets rationale as the llm block:
                            # pass key + base_url explicitly so mem0 doesn't
                            # fall back to an empty OPENAI_API_KEY env var.
                            # Embeddings may live on a DIFFERENT vendor than
                            # chat (DeepSeek has no /embeddings endpoint), so
                            # this block uses the EMBEDDING_* provider.
                            "api_key": settings.EMBEDDING_API_KEY,
                            "openai_base_url": settings.EMBEDDING_BASE_URL,
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

    async def search(self, user_id: str | None, query: str, filters: dict | None = None) -> str:
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
        # Offline mode (no Postgres): skip mem0 entirely to avoid 30s pgvector
        # connection timeouts per request.
        if not _MEMORY_SERVICE_ENABLED:
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
                search_kwargs: dict = {"user_id": str(user_id), "query": query}
                if filters:
                    search_kwargs["filters"] = filters
                results = await memory.search(**search_kwargs)
                # Filter by relevance: only include memories whose cosine
                # similarity >= threshold. Without this, mem0 returns ALL
                # stored memories regardless of relevance, causing the LLM
                # to be biased by irrelevant context (e.g., user asks about
                # "半导体" but sees memories about "600519" and responds
                # about the wrong stock).
                relevant_memories = []
                for r in results.get("results", []):
                    score = r.get("score")
                    # If score is unavailable (some backends don't return
                    # it), include the result for backward compatibility.
                    if score is None or float(score) >= _MEMORY_RELEVANCE_THRESHOLD:
                        relevant_memories.append(r["memory"])
                if not relevant_memories:
                    logger.debug(
                        "memory_search_all_below_threshold",
                        user_id=user_id,
                        total=len(results.get("results", [])),
                        threshold=_MEMORY_RELEVANCE_THRESHOLD,
                    )
                return "\n".join([f"* {m}" for m in relevant_memories])

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
        # Offline mode (no Postgres): skip mem0 entirely.
        if not _MEMORY_SERVICE_ENABLED:
            return
        # M-16 fix: short-circuit when pgvector is persistently down.
        if _memory_breaker.is_open:
            logger.debug("memory_add_short_circuited", user_id=user_id)
            return
        # Phase 3: filter noise roles before persistence so mem0 extracts
        # memories from real dialogue only, not tool results or system
        # prompts. Without this, mem0's extraction LLM distills tool-returned
        # price/news data into misleading memories (e.g. "用户关注贵州茅台"
        # extracted from a quote payload while answering a 半导体 question),
        # which then pollute retrieval and bias the next answer.
        messages = _filter_messages_for_memory(messages)
        if not messages:
            logger.debug("memory_add_skipped_no_dialogue", user_id=user_id)
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

    async def clear_user_memory(self, user_id: str | None) -> None:
        """Delete all long-term memories for a user and invalidate caches.

        Args:
            user_id: The user whose memories should be cleared. No-op when
                ``None`` (anonymous sessions have no long-term memory).
        """
        if user_id is None:
            return
        if _memory_breaker.is_open:
            logger.debug("memory_clear_short_circuited", user_id=user_id)
            return
        try:
            memory = await self._get_memory()
            await memory.delete_all(user_id=str(user_id))
            # Critical: invalidate the semantic cache so subsequent searches
            # don't return the stale cached memory string. Without this, the
            # clear has no observable effect until the cache TTL expires.
            await semantic_cache.invalidate_user("memory", str(user_id))
            _memory_breaker.record_success()
            logger.info("user_memory_cleared", user_id=user_id)
        except Exception as e:
            _memory_breaker.record_failure()
            logger.exception("failed_to_clear_user_memory", user_id=user_id, error=str(e))


memory_service = MemoryService()
