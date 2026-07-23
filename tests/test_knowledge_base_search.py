"""Regression tests for knowledge base search.

These tests are intentionally hermetic:

* They do NOT call any external embedding API (DashScope / OpenAI). A
  deterministic, local bag-of-character-bigram embedder is used so the same
  text always maps to the same vector and topically-related text scores higher
  than unrelated text.
* They do NOT touch the real pgvector store. The service's ``_memory`` backend
  is replaced with an in-memory fake, and the Plan A consistency guard's data
  source (``_stored_embedding_models``) is redirected to that fake as well, so
  the *real* guard logic still runs -- just against the fake store.

This validates the retrieval link (chunk -> embed -> store -> query -> rank)
and relevance ordering, plus the Plan A embedding-model-mismatch guard, without
any network or database dependency.
"""

from __future__ import annotations

import hashlib
import math
import re
from types import SimpleNamespace

import pytest

from app.core.config import settings

# Importing app.main first seeds a safe module-init order; importing the KB
# service directly as the first app.* import can otherwise trip a latent
# circular import in app.utils (same reason other tests go through app.main).
from app.main import app  # noqa: F401
from app.services.knowledge_base_service import (
    EmbeddingModelMismatchError,
    KnowledgeBaseService,
    knowledge_base_service,
)

# Model name stamped on every chunk by the service (mirrors a real config). The
# guard compares stored models against this, so the fake store must use it too.
FAKE_MODEL = "test-deterministic-embedder-v1"

TEST_USER_ID = 990001

# --- deterministic local embedder (no external dependency) -----------------


def _tokens(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = []
    # CJK character bigrams capture most of the topical signal for Chinese.
    cjk = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i] + cjk[i + 1])
    # ASCII words (incl. numbers) keep latin-token topicality.
    for w in re.findall(r"[a-z0-9]+", text):
        tokens.append("w:" + w)
    return tokens


def _embed(text: str, dim: int = 512) -> list[float]:
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if ((h >> 20) & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- in-memory fake mem0 backend -------------------------------------------


class FakeMemory:
    """Minimal in-memory stand-in for mem0's AsyncMemory.

    It implements only what ``KnowledgeBaseService`` uses (add / search /
    get_all / delete) and embeds with the deterministic local embedder above,
    so relevance ranking is meaningful and reproducible.
    """

    def __init__(self) -> None:
        """Initialize an empty in-memory store."""
        self._records: list[dict] = []
        self._seq = 0

    async def add(self, messages, user_id, metadata, infer=False, **kwargs):
        """Store a chunk, embedding it with the deterministic local embedder."""
        content = messages[0]["content"]
        self._seq += 1
        self._records.append(
            {
                "id": f"mem_{self._seq}",
                "user_id": str(user_id),
                "content": content,
                "metadata": dict(metadata),
                "embedding_model": metadata.get("embedding_model"),
                "vector": _embed(content),
            }
        )
        return {"results": [{"id": f"mem_{self._seq}"}]}

    async def search(self, user_id, query, limit=100, **kwargs):
        """Cosine-rank all of the user's chunks for the query."""
        qv = _embed(query)
        scored = [(rec, _cosine(rec["vector"], qv)) for rec in self._records if str(rec["user_id"]) == str(user_id)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return {
            "results": [
                {
                    "id": rec["id"],
                    "memory": rec["content"],
                    "score": score,
                    "metadata": rec["metadata"],
                }
                for rec, score in scored[:limit]
            ]
        }

    async def get_all(self, user_id, limit=100, **kwargs):
        """Return all stored chunks for a user."""
        recs = [r for r in self._records if str(r["user_id"]) == str(user_id)]
        return {"results": [{"id": r["id"], "memory": r["content"], "metadata": r["metadata"]} for r in recs[:limit]]}

    async def delete(self, memory_id, **kwargs):
        """Remove a chunk by id."""
        self._records = [r for r in self._records if r["id"] != memory_id]

    def models_for(self, user_id) -> set:
        """Return the set of embedding models present for a user."""
        return {
            r["embedding_model"] for r in self._records if str(r["user_id"]) == str(user_id) and r["embedding_model"]
        }

    def inject_model_mismatch(self, user_id) -> None:
        """Add a record whose embedding model differs from the config.

        Used to exercise the Plan A guard: the next search must be blocked.
        """
        self._seq += 1
        self._records.append(
            {
                "id": f"mem_{self._seq}",
                "user_id": str(user_id),
                "content": "unrelated filler content xyz",
                "metadata": {"embedding_model": "old-incompatible-model"},
                "embedding_model": "old-incompatible-model",
                "vector": _embed("unrelated filler content xyz"),
            }
        )


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def fake_memory(monkeypatch):
    """Wire an in-memory KB backend and redirect the Plan A guard.

    Fully hermetic: no DB and no external embedding API are touched.
    """
    fm = FakeMemory()

    monkeypatch.setattr(settings, "ENABLE_KNOWLEDGE_BASE", True)
    monkeypatch.setattr(settings, "LONG_TERM_MEMORY_EMBEDDER_MODEL", FAKE_MODEL)

    # Point the real guard at the fake store instead of pgvector.
    async def _fake_stored(self, user_id):
        return fm.models_for(user_id)

    monkeypatch.setattr(KnowledgeBaseService, "_stored_embedding_models", _fake_stored)

    # Replace the mem0 backend with the fake.
    saved = knowledge_base_service._memory
    knowledge_base_service._memory = fm
    yield fm
    knowledge_base_service._memory = saved


async def _upload(filename: str, content: str) -> str:
    """Upload one document via the real service and return its doc_id."""
    result = await knowledge_base_service.upload_document(
        user_id=TEST_USER_ID,
        filename=filename,
        content=content.encode("utf-8"),
    )
    return result["doc_id"]


@pytest.fixture
async def uploaded_docs(fake_memory):
    """Upload three single-topic documents and return their doc_ids."""
    docs = {
        "value_investing": (
            "价值投资导论.md",
            "巴菲特倡导价值投资，强调安全边际与长期持有，源于格雷厄姆的投资哲学，关注企业内在价值而非市场短期波动。",
        ),
        "cooking": (
            "家常菜谱.md",
            "番茄炒蛋是一道简单的家常菜，需要鸡蛋、番茄和油盐，先炒蛋再炒番茄，最后混合调味即可出锅。",
        ),
        "machine_learning": (
            "机器学习笔记.md",
            "机器学习中的神经网络通过反向传播算法训练，利用梯度下降优化损失函数，是深度学习的基础。",
        ),
    }
    doc_ids = {}
    for key, (filename, content) in docs.items():
        doc_ids[key] = await _upload(filename, content)
    return doc_ids


# --- retrieval relevance tests (core P2) -----------------------------------


async def test_upload_stamps_embedding_model(fake_memory):
    """upload_document stamps the active embedding model onto each chunk."""
    await _upload("价值投资导论.md", "巴菲特 价值投资 安全边际 格雷厄姆。")
    assert fake_memory.models_for(TEST_USER_ID) == {FAKE_MODEL}


async def test_relevant_query_returns_correct_doc_first(fake_memory, uploaded_docs):
    """A topically-relevant query returns the correct document first."""
    results = await knowledge_base_service.search(str(TEST_USER_ID), "巴菲特的价值投资理念", limit=5)
    assert results, "expected at least one result"
    assert results[0]["doc_id"] == uploaded_docs["value_investing"]
    # Top score should be meaningfully positive (query matches the doc). The
    # exact magnitude depends on the local embedder; ranking is the real check.
    assert results[0]["score"] > 0.2


async def test_exact_title_query_returns_that_doc(fake_memory, uploaded_docs):
    """A query matching a document title returns that document."""
    results = await knowledge_base_service.search(str(TEST_USER_ID), "价值投资导论", limit=5)
    assert results[0]["doc_id"] == uploaded_docs["value_investing"]
    assert results[0]["score"] > 0.2


async def test_unrelated_query_is_discriminated(fake_memory, uploaded_docs):
    """An out-of-corpus query scores lower than an in-corpus relevant query."""
    relevant = await knowledge_base_service.search(str(TEST_USER_ID), "巴菲特的价值投资理念", limit=5)
    unrelated = await knowledge_base_service.search(str(TEST_USER_ID), "如何组装一辆自行车", limit=5)
    # An in-corpus relevant query must out-score an out-of-corpus query.
    assert relevant[0]["score"] > unrelated[0]["score"]
    # The unrelated query should not falsely surface the value-investing doc
    # with high confidence.
    assert unrelated[0]["score"] < 0.5


async def test_empty_query_returns_no_results(fake_memory, uploaded_docs):
    """Whitespace/empty queries return no results."""
    results = await knowledge_base_service.search(str(TEST_USER_ID), "   ", limit=5)
    assert results == []


# --- Plan A guard regression (real guard code, fake store) -----------------


async def test_search_blocked_on_embedding_model_mismatch(fake_memory):
    """Search is blocked (raises) when stored and config models diverge."""
    # No data yet -> consistent (empty set), search allowed and returns [].
    assert await knowledge_base_service.search(str(TEST_USER_ID), "anything", limit=5) == []

    # Inject a chunk whose model differs from the current config.
    fake_memory.inject_model_mismatch(TEST_USER_ID)
    with pytest.raises(EmbeddingModelMismatchError):
        await knowledge_base_service.search(str(TEST_USER_ID), "anything", limit=5)


# --- API layer: 409 translation + happy path (hermetic) --------------------


async def test_search_api_returns_409_on_model_mismatch(fake_memory, async_client, monkeypatch):
    """The API translates the guard exception into HTTP 409."""
    from app.api.v1.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=TEST_USER_ID)

    async def _raise(*args, **kwargs):
        raise EmbeddingModelMismatchError(
            f"Knowledge base embedding model mismatch for {TEST_USER_ID}: "
            f"stored=['old'] current={FAKE_MODEL}. Re-embed the knowledge base."
        )

    monkeypatch.setattr(knowledge_base_service, "search", _raise)
    try:
        resp = await async_client.get("/api/v1/knowledge-base/search", params={"q": "anything", "limit": 5})
        assert resp.status_code == 409
        assert "embedding model mismatch" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


async def test_search_api_happy_path_returns_ranked_results(fake_memory, uploaded_docs, async_client, monkeypatch):
    """The API returns ranked results for a happy-path query."""
    from app.api.v1.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=TEST_USER_ID)
    try:
        resp = await async_client.get(
            "/api/v1/knowledge-base/search",
            params={"q": "巴菲特的价值投资理念", "limit": 5},
        )
        assert resp.status_code == 200
        body = resp.json()
        results = body if isinstance(body, list) else body.get("results", [])
        assert results, "expected ranked results from the API"
        assert results[0]["doc_id"] == uploaded_docs["value_investing"]
    finally:
        app.dependency_overrides.clear()
