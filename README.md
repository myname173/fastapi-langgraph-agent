# FastAPI LangGraph Agent Template

A production-ready template for building AI agent backends with FastAPI and LangGraph. Handles the hard parts — stateful conversations, long-term memory, tool calling, observability, rate limiting, auth — so you can focus on your agent logic.

**Built for AI engineers** who want a solid foundation, not a tutorial project.

---

## Powered by Atlas Cloud — Drop-in LLM Backend for LangGraph Agents

<div align="center">
  <a href="https://www.atlascloud.ai/?utm_source=github&utm_medium=link&utm_campaign=fastapi-langgraph-agent-production-ready-template">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/atlas-cloud-logo-dark.png"/>
      <img src="docs/atlas-cloud-logo.png" alt="Atlas Cloud" width="200"/>
    </picture>
  </a>
</div>

[**Atlas Cloud**](https://www.atlascloud.ai/?utm_source=github&utm_medium=link&utm_campaign=fastapi-langgraph-agent-production-ready-template) provides an **OpenAI-compatible LLM API** that integrates seamlessly into this FastAPI + LangGraph template — no code changes to your agent graph needed. Just swap `OPENAI_BASE_URL` and `OPENAI_API_KEY` to access **DeepSeek, Qwen, GLM, Kimi, MiniMax, Gemini, Claude, GPT** and more through a single unified endpoint.

The `LLMRegistry` in this template uses `langchain_openai.ChatOpenAI` — Atlas Cloud is wire-compatible, so you get instant access to 59+ curated reasoning models without touching any LangGraph logic.

### Quick Setup

**Step 1 — Get your free API key:** [atlascloud.ai/console/coding-plan](https://www.atlascloud.ai/console/coding-plan)

**Step 2 — Update `.env.development`:**

```env
OPENAI_API_KEY=<your-atlascloud-key>
OPENAI_BASE_URL=https://api.atlascloud.ai/v1
DEFAULT_LLM_MODEL=deepseek-ai/deepseek-v4-pro
```

**Step 3 — Or use directly in code:**

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="deepseek-ai/deepseek-v4-pro",
    openai_api_base="https://api.atlascloud.ai/v1",
    openai_api_key="<your-atlascloud-key>",
    max_tokens=512,  # reasoning model requires max_tokens >= 512
)
```

This works as a drop-in replacement anywhere `ChatOpenAI` is used in your LangGraph agent — including the `LLMRegistry`, the circular fallback service, and mem0 long-term memory.

<details>
<summary>📋 Full model catalog (59 LLMs available)</summary>

| Model ID | Provider |
|---|---|
| `deepseek-ai/DeepSeek-V3-0324` | DeepSeek |
| `deepseek-ai/deepseek-r1-0528` | DeepSeek |
| `deepseek-ai/DeepSeek-V3.1` | DeepSeek |
| `deepseek-ai/DeepSeek-V3.1-Terminus` | DeepSeek |
| `deepseek-ai/DeepSeek-V3.2-Exp` | DeepSeek |
| `deepseek-ai/deepseek-v3.2` | DeepSeek |
| `qwen/qwen3-32b` | Alibaba Qwen |
| `qwen/qwen3-8b` | Alibaba Qwen |
| `qwen/qwen3-235b-a22b-thinking-2507` | Alibaba Qwen |
| `qwen/qwen3-30b-a3b` | Alibaba Qwen |
| `qwen/qwen3-30b-a3b-thinking-2507` | Alibaba Qwen |
| `Qwen/Qwen3-Coder` | Alibaba Qwen |
| `Qwen/Qwen3-235B-A22B-Instruct-2507` | Alibaba Qwen |
| `Qwen/Qwen3-Next-80B-A3B-Instruct` | Alibaba Qwen |
| `Qwen/Qwen3-Next-80B-A3B-Thinking` | Alibaba Qwen |
| `Qwen/Qwen3-30B-A3B-Instruct-2507` | Alibaba Qwen |
| `Qwen/Qwen3-VL-235B-A22B-Instruct` | Alibaba Qwen |
| `moonshotai/Kimi-K2-Instruct` | Moonshot AI |
| `moonshotai/Kimi-K2-Instruct-0905` | Moonshot AI |
| `moonshotai/Kimi-K2-Thinking` | Moonshot AI |
| `moonshotai/kimi-k2.5` | Moonshot AI |
| `zai-org/GLM-4.6` | Zhipu AI |
| `zai-org/glm-4.7` | Zhipu AI |
| `MiniMaxAI/MiniMax-M2` | MiniMax |
| `minimaxai/minimax-m2.1` | MiniMax |
| `google/gemini-2.5-flash` | Google |
| `google/gemini-2.5-flash-preview-202509` | Google |
| `google/gemini-2.5-flash-lite` | Google |
| `google/gemini-2.5-flash-lite-preview-202509` | Google |
| `google/gemini-2.5-pro` | Google |
| `google/gemini-3-flash-preview` | Google |
| `google/gemini-2.0-flash` | Google |
| `google/gemini-2.0-flash-lite` | Google |
| `openai/gpt-5.1` | OpenAI |
| `openai/gpt-5.1-chat` | OpenAI |
| `openai/gpt-5.1-codex` | OpenAI |
| `openai/gpt-5.1-codex-mini` | OpenAI |
| `openai/gpt-5.1-codex-max` | OpenAI |
| `openai/gpt-4o` | OpenAI |
| `openai/gpt-4o-mini` | OpenAI |
| `openai/gpt-4.1` | OpenAI |
| `openai/gpt-4.1-mini` | OpenAI |
| `openai/gpt-4.1-nano` | OpenAI |
| `openai/o1` | OpenAI |
| `openai/o3` | OpenAI |
| `openai/o3-mini` | OpenAI |
| `openai/o4-mini` | OpenAI |
| `openai/o3-pro` | OpenAI |
| `openai/gpt-5` | OpenAI |
| `openai/gpt-5-chat` | OpenAI |
| `openai/gpt-5-codex` | OpenAI |
| `openai/gpt-5-mini` | OpenAI |
| `openai/gpt-5-nano` | OpenAI |
| `openai/gpt-5-pro` | OpenAI |
| `openai/gpt-5.2` | OpenAI |
| `openai/gpt-5.2-chat` | OpenAI |
| `anthropic/claude-sonnet-4-20250514` | Anthropic |
| `anthropic/claude-haiku-4.5-20251001` | Anthropic |
| `anthropic/claude-sonnet-4.5-20250929` | Anthropic |
| `anthropic/claude-opus-4.1-20250805` | Anthropic |
| `anthropic/claude-opus-4-20250514` | Anthropic |
| `anthropic/claude-opus-4.5-20251101` | Anthropic |

[View live model list →](https://www.atlascloud.ai/?utm_source=github&utm_medium=link&utm_campaign=fastapi-langgraph-agent-production-ready-template)

</details>

---

## What's included

- **LangGraph** stateful agent with checkpointing, tool calling, and human-in-the-loop support
- **Long-term memory** via mem0 + pgvector — semantic search per user, cache-backed
- **LLM service** with circular model fallback, exponential backoff retries, and total timeout budget
- **Langfuse** tracing on all LLM calls; Prometheus metrics + Grafana dashboards
- **JWT auth** with session management; rate limiting via slowapi
- **Alembic** migrations; optional Valkey/Redis cache layer
- **Structured logging** with request/session/user context on every line

## Quickstart

```bash
git clone <repo-url> my-agent && cd my-agent
cp .env.example .env.development   # fill in your keys
make install
make docker-up                     # starts API + PostgreSQL
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) to see the interactive API.

> For local development without Docker see [docs/getting-started.md](docs/getting-started.md).

## Documentation

| Guide | What it covers |
|---|---|
| [Getting Started](docs/getting-started.md) | Prerequisites, local setup, first API call |
| [Architecture](docs/architecture.md) | System design, request flow, component diagrams |
| [Configuration](docs/configuration.md) | All environment variables with defaults |
| [Authentication](docs/authentication.md) | JWT flow, sessions, endpoint reference |
| [Database & Migrations](docs/database.md) | Schema, Alembic migrations, pgvector |
| [LLM Service](docs/llm-service.md) | Models, retries, fallback, timeout budget |
| [Memory](docs/memory.md) | mem0 long-term memory, cache layer |
| [Observability](docs/observability.md) | Langfuse, structured logging, Prometheus, profiling |
| [Evaluation](docs/evaluation.md) | Eval framework, custom metrics, reports |
| [Docker](docs/docker.md) | Docker, Compose, full monitoring stack |
| [LLM Cost Tracking](docs/llm-cost-tracking.md) | Per-agent cost metrics, PromQL examples, pricing table |
| [Knowledge Base](docs/knowledge-base.md) | Document upload + RAG-enhanced code generation |
| [WebSocket SDLC](docs/websocket.md) | Bidirectional approve/reject endpoint, protocol reference |

## Feature catalog

The template ships with a baseline set of always-on features and 30+ Feature Flags for opt-in enhancements. Every flag is listed in [`.env.example`](.env.example) with its default value and a one-line description.

### Always-on (default `true` or unconditional)

| Area | Feature | Notes |
|---|---|---|
| Agent runtime | LangGraph stateful agent with checkpointing | `AsyncPostgresSaver` persistence |
| Agent runtime | Tool calling + human-in-the-loop (`interrupt()`) | |
| LLM service | Circular model fallback + tenacity retries + timeout budget | See [docs/llm-service.md](docs/llm-service.md) |
| Memory | mem0 long-term memory per user | pgvector-backed |
| Auth | JWT sessions, password hashing | See [docs/authentication.md](docs/authentication.md) |
| Rate limiting | slowapi per-route decorators | |
| Observability | Structured logging (structlog) | Always on |
| Observability | Prometheus metrics + Grafana dashboards | `/metrics` endpoint |
| Database | Alembic migrations, async operations | See [docs/database.md](docs/database.md) |
| SDLC orchestrator | Smart Coder (semantic code-index context) | `ENABLE_SMART_CODER=true` |
| SDLC orchestrator | Knowledge graph (AST-based) | `ENABLE_KNOWLEDGE_GRAPH=true` |
| SDLC orchestrator | Smart search (hybrid vector + graph) | `ENABLE_SMART_SEARCH=true` |
| SDLC orchestrator | Code index (pgvector) | `CODE_INDEX_ENABLED=true` |
| SDLC orchestrator | Smart editing with post-edit validation | `ENABLE_SMART_EDITING=true` |
| SDLC orchestrator | Resource quota + script execution | `ENABLE_RESOURCE_QUOTA=true`, `ENABLE_SCRIPT_EXECUTION=true` |
| SDLC orchestrator | Security scan + audit log + advanced monitoring | `ENABLE_SECURITY_SCAN=true`, `ENABLE_AUDIT_LOG=true`, `ENABLE_ADVANCED_MONITORING=true` |
| SDLC orchestrator | Multi-agent handoff tracing | `ENABLE_HANDOFF_TRACING=true` |

### Opt-in (default `false` — set to `true` to enable)

| Flag | What it enables | See |
|---|---|---|
| `USE_SDLC_ORCHESTRATOR` | Multi-agent SDLC workflow (planner/coder/tester/reviewer/supervisor) | [docs/architecture.md](docs/architecture.md) |
| `LANGFUSE_TRACING_ENABLED` | LLM call traces in Langfuse UI | [docs/observability.md](docs/observability.md) |
| `ENABLE_MULTIMODAL` | Image analysis in agent tasks | |
| `ENABLE_MCP_CLIENT` | Load external MCP tools for coder/tester (prefixed `mcp_`) | |
| `ENABLE_WEBSOCKET` | Bidirectional WebSocket for approve/reject | [docs/websocket.md](docs/websocket.md) |
| `ENABLE_KNOWLEDGE_BASE` | Document upload + RAG-enhanced coder | [docs/knowledge-base.md](docs/knowledge-base.md) |
| `ENABLE_ASYNC_DB` | Native async DB driver (psycopg3) for 3-5x QPS | |
| `SEMANTIC_CACHE_ENABLED` | Semantic LLM response cache (requires Valkey/Redis) | |

## Enabling optional features

All opt-in features use the same pattern: set the flag to `true` in your environment file (`.env.development`, `.env.staging`, `.env.production`), then restart the API. No code changes required.

### Example: enable the SDLC orchestrator + knowledge base

```env
USE_SDLC_ORCHESTRATOR=true
ENABLE_KNOWLEDGE_BASE=true
```

For PDF support in the knowledge base, install the optional dependency:

```bash
uv pip install pymupdf
```

Then upload a document:

```bash
curl -X POST http://localhost:8000/api/v1/knowledge-base/upload \
  -H "Authorization: Bearer $JWT" \
  -F "file=@api-spec.md"
```

### Example: enable WebSocket for real-time approve/reject

```env
USE_SDLC_ORCHESTRATOR=true
ENABLE_WEBSOCKET=true
```

Connect from JavaScript:

```javascript
const ws = new WebSocket(
  `ws://localhost:8000/api/v1/ws/sdlc/${threadId}?token=${jwt}`
);
ws.onopen = () => ws.send(JSON.stringify({command: "approve", payload: {}}));
```

See [docs/websocket.md](docs/websocket.md) for the full protocol reference.

### Example: enable LLM cost tracking

Cost tracking is always on when `ENABLE_ADVANCED_MONITORING=true` (default). No additional setup. Scrape the metrics endpoint:

```bash
curl http://localhost:8000/metrics | grep sdlc_llm
```

See [docs/llm-cost-tracking.md](docs/llm-cost-tracking.md) for PromQL query examples.

### Zero-regression guarantee

Every opt-in feature is independently removable:

- The router/handler is conditionally registered in `app/api/v1/api.py` only when the flag is true.
- Service imports in agent nodes are lazy-fault-tolerant — agent falls back to the baseline path when a feature is disabled.
- No database migration is required to disable an opt-in feature.

## Project structure

```
app/
  api/v1/          # Route handlers
  core/
    langgraph/     # Agent graph + tools
    prompts/       # System prompt template
    cache.py       # Valkey/Redis + in-memory fallback
    config.py      # Settings
    middleware.py  # Metrics, logging context, profiling
    limiter.py     # Rate limiting
  models/          # SQLModel ORM models
  schemas/         # Pydantic request/response schemas
  services/        # LLM, database, memory services
alembic/           # Database migrations
evals/             # LLM evaluation framework
```

## Contributing

PRs welcome. Please read [docs/getting-started.md](docs/getting-started.md) to get your environment set up, then follow the coding conventions in [AGENTS.md](AGENTS.md).

Report security issues privately — see [SECURITY.md](SECURITY.md).

## License

See [LICENSE](LICENSE).

## FAQ

### General

**What is this template?**
A production-ready foundation for AI agent backends built on FastAPI + LangGraph. It bundles the components you'd otherwise wire up by hand: stateful conversations, long-term memory, tool calling, observability, rate limiting, and JWT auth.

**How does this differ from a basic LangGraph setup?**
The base LangGraph quickstart stops at "agent runs locally". This template adds Alembic migrations, mem0 + pgvector long-term memory, Langfuse tracing, Prometheus + Grafana dashboards, JWT sessions, slowapi rate limiting, structured logging with per-request context, and a circular-fallback LLM service — production concerns you'd otherwise build separately.

### Setup & Configuration

**Do I need Docker?**
Recommended but not required. `make docker-up` starts the API + PostgreSQL together. For local-only setup see [docs/getting-started.md](docs/getting-started.md).

**Which LLM providers are supported?**
Today: **OpenAI only** via the `LLMRegistry` in `app/services/llm/registry.py`. Multi-provider support (Anthropic, Google, OpenRouter) via LangChain's `init_chat_model` is planned — see [#51](https://github.com/wassim249/fastapi-langgraph-agent-production-ready-template/issues/51). Configure your model via `DEFAULT_LLM_MODEL` in `.env.development`.

**How do I configure long-term memory?**
Long-term memory is self-hosted: mem0 runs in-process and persists into your existing PostgreSQL via pgvector — there is no separate mem0 cloud account or API key. You only need a working `OPENAI_API_KEY` (used for fact extraction + embeddings) and the pgvector extension enabled. See [docs/memory.md](docs/memory.md) for details.

### Development

**How do I add a custom tool?**
Drop a LangChain `@tool`-decorated function in `app/core/langgraph/tools/` and register it in the `tools` list exported from that package. The agent picks it up on next start; no graph changes needed.

**How does the LLM service handle failures?**
Two layers: (1) per-call exponential-backoff retry via `tenacity`, (2) **circular fallback** — if the active model exhausts its retries, the service rotates to the next model in `LLMRegistry` and continues. A total timeout budget caps the whole call so latency stays bounded. See [docs/llm-service.md](docs/llm-service.md).

**Can I use this without Langfuse?**
Yes. Set `LANGFUSE_TRACING_ENABLED=false` (or omit the Langfuse keys). The agent runs unchanged; structured logs still capture request/session/user context.

### Troubleshooting

**The API won't start**
- Ensure PostgreSQL is running (`make docker-up` brings it up alongside the API)
- Confirm `.env.development` exists — copy from `.env.example` and fill in required keys
- Apply migrations: `make migrate`

**Memory / semantic search returns nothing**
- Verify the `pgvector` extension is enabled in your PostgreSQL instance
- Confirm `OPENAI_API_KEY` is valid (mem0 calls OpenAI for fact extraction + embeddings)
- Check `LONG_TERM_MEMORY_MODEL` and `LONG_TERM_MEMORY_EMBEDDER_MODEL` are set in `.env.development`

**Rate limiting is too aggressive**
Limits are defined in `app/core/limiter.py` (slowapi). Adjust per-route decorators or the default rate in that file. See [docs/configuration.md](docs/configuration.md) for the related env vars.
