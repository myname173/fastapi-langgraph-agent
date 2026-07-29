"""Application configuration management.

This module handles environment-specific configuration loading, parsing, and management
for the application. It includes environment detection, .env file loading, and
configuration value parsing.
"""

import json
import os
import tempfile
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv


# Define environment types
class Environment(str, Enum):
    """Application environment types.

    Defines the possible environments the application can run in:
    development, staging, production, and test.
    """

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


# Determine environment
def get_environment() -> Environment:
    """Get the current environment.

    Returns:
        Environment: The current environment (development, staging, production, or test)
    """
    match os.getenv("APP_ENV", "development").lower():
        case "production" | "prod":
            return Environment.PRODUCTION
        case "staging" | "stage":
            return Environment.STAGING
        case "test":
            return Environment.TEST
        case _:
            return Environment.DEVELOPMENT


# Load appropriate .env file based on environment
def load_env_file():
    """Load environment-specific .env file."""
    env = get_environment()
    print(f"Loading environment: {env}")
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

    # Define env files in priority order
    env_files = [
        os.path.join(base_dir, f".env.{env.value}.local"),
        os.path.join(base_dir, f".env.{env.value}"),
        os.path.join(base_dir, ".env.local"),
        os.path.join(base_dir, ".env"),
    ]

    # Load the first env file that exists
    for env_file in env_files:
        if os.path.isfile(env_file):
            load_dotenv(dotenv_path=env_file)
            print(f"Loaded environment from {env_file}")
            return env_file

    # Fall back to default if no env file found
    return None


ENV_FILE = load_env_file()


# Parse list values from environment variables
def parse_list_from_env(env_key, default=None):
    """Parse a comma-separated list from an environment variable."""
    value = os.getenv(env_key)
    if not value:
        return default or []

    # Remove quotes if they exist
    value = value.strip("\"'")
    # Handle single value case
    if "," not in value:
        return [value]
    # Split comma-separated values
    return [item.strip() for item in value.split(",") if item.strip()]


def get_secret(name: str, default: str = "") -> str:
    """Read a secret from a file path or environment variable.

    Supports the Docker secrets / 12-factor ``_FILE`` convention:
    if ``{name}_FILE`` is set, read the secret value from that file path
    (stripping surrounding whitespace). Otherwise fall back to the ``{name}``
    environment variable directly.

    Args:
        name: Base name of the secret (e.g. ``"OPENAI_API_KEY"``).
        default: Default value if neither ``{name}_FILE`` nor ``{name}`` is set.

    Returns:
        The secret value, or ``default`` if unavailable.
    """
    file_path = os.getenv(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            # File missing/unreadable — fall through to env var lookup.
            pass
    return os.getenv(name, default)


# Parse dict of lists from environment variables with prefix
def parse_dict_of_lists_from_env(prefix, default_dict=None):
    """Parse dictionary of lists from environment variables with a common prefix."""
    result = default_dict or {}

    # Look for all env vars with the given prefix
    for key, value in os.environ.items():
        if key.startswith(prefix):
            endpoint = key[len(prefix) :].lower()  # Extract endpoint name
            # Parse the values for this endpoint
            if value:
                value = value.strip("\"'")
                if "," in value:
                    result[endpoint] = [item.strip() for item in value.split(",") if item.strip()]
                else:
                    result[endpoint] = [value]

    return result


class Settings:
    """Application settings without using pydantic."""

    def __init__(self):
        """Initialize application settings from environment variables.

        Loads and sets all configuration values from environment variables,
        with appropriate defaults for each setting. Also applies
        environment-specific overrides based on the current environment.
        """
        # Set the environment
        self.ENVIRONMENT = get_environment()

        # Application Settings
        self.PROJECT_NAME = os.getenv("PROJECT_NAME", "FastAPI LangGraph Template")
        self.VERSION = os.getenv("VERSION", "1.0.0")
        self.DESCRIPTION = os.getenv(
            "DESCRIPTION", "A production-ready FastAPI template with LangGraph and Langfuse integration"
        )
        self.API_V1_STR = os.getenv("API_V1_STR", "/api/v1")
        self.DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "t", "yes")

        # CORS Settings
        # H-5 fix: never default to ["*"] — browsers reject the
        # Access-Control-Allow-Origin: * + Access-Control-Allow-Credentials:
        # true combination, which breaks authenticated requests from the
        # Streamlit UI (and any credentialed cross-origin client). Default
        # to the common local dev origins instead; production must set
        # ALLOWED_ORIGINS explicitly in the environment.
        self.ALLOWED_ORIGINS = parse_list_from_env(
            "ALLOWED_ORIGINS",
            ["http://localhost:3000", "http://localhost:8000", "http://localhost:8501"],
        )

        # Langfuse Configuration
        self.LANGFUSE_TRACING_ENABLED = os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() in (
            "true",
            "1",
            "t",
            "yes",
        )
        self.LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self.LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
        self.LANGFUSE_HOST = os.getenv("LANGFUSE_BASE_URL", "https://jp.cloud.langfuse.com")

        # LangGraph Configuration
        # 支持 Docker secrets：优先读取 OPENAI_API_KEY_FILE 指向的文件内容，
        # 回退到 OPENAI_API_KEY 环境变量。生产环境通过 docker-compose secrets 注入。
        self.OPENAI_API_KEY = get_secret("OPENAI_API_KEY", "")
        # OpenAI-compatible base URL. Set this to use non-OpenAI providers
        # (e.g. Alibaba DashScope/Bailian, SiliconFlow, Azure OpenAI).
        #   Alibaba Bailian:  https://dashscope.aliyuncs.com/compatible-mode/v1
        #   SiliconFlow:      https://api.siliconflow.cn/v1
        #   OpenAI (default): https://api.openai.com/v1
        self.OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.DEFAULT_LLM_MODEL = os.getenv("DEFAULT_LLM_MODEL", "")
        # Empty default forces explicit configuration in production/staging —
        # validate_security() Tier 3 rejects an empty value there. Test/dev
        # envs are exempt. See .env.example for provider presets.
        # Comma-separated list of model names in the LLMRegistry fallback chain.
        # DEFAULT_LLM_MODEL must be in this list when set.
        self.LLM_MODELS = parse_list_from_env("LLM_MODELS", ["gpt-5-mini", "gpt-5.4", "gpt-5"])
        # Per-model provider overrides for multi-provider routing (e.g. local
        # vLLM + cloud API). JSON dict: {"model_name": {"base_url": "...",
        # "api_key": "...", "max_tokens": 100}}. Empty or invalid JSON degrades
        # gracefully to {} (all models use shared OPENAI_BASE_URL).
        _raw_overrides = os.getenv("LLM_PROVIDER_OVERRIDES", "")
        try:
            self.LLM_PROVIDER_OVERRIDES = json.loads(_raw_overrides) if _raw_overrides else {}
            if not isinstance(self.LLM_PROVIDER_OVERRIDES, dict):
                self.LLM_PROVIDER_OVERRIDES = {}
        except (json.JSONDecodeError, TypeError):
            self.LLM_PROVIDER_OVERRIDES = {}
        # User-selectable coder models for the SDLC orchestrator (exposed via
        # GET /api/v1/sdlc/models and the coder view dropdown). Invariants:
        #   - every entry must be resolvable by LLMRegistry.get() at runtime
        #     (present in LLM_MODELS or registered via ModelRouter.ensure_model);
        #     coder_node calls get_llm_for(model_name) with NO fallback.
        #   - every entry should set max_tokens:8192 in LLM_PROVIDER_OVERRIDES,
        #     otherwise the default MAX_TOKENS=2000 truncates the multi-file
        #     CoderOutput JSON and the workflow熔断 trips on parse failure.
        self.CODER_ELIGIBLE_MODELS = [
            m.strip()
            for m in os.getenv(
                "CODER_ELIGIBLE_MODELS",
                "qwen3-coder-plus,qwen3.7-max,qwen3.7-max-preview,qwen3.7-plus-2026-05-26,glm-5.2",
            ).split(",")
            if m.strip()
        ]
        self.SESSION_NAMING_ENABLED = os.getenv("SESSION_NAMING_ENABLED", "true").lower() == "true"
        self.DEFAULT_LLM_TEMPERATURE = float(os.getenv("DEFAULT_LLM_TEMPERATURE", "0.2"))
        self.MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2000"))
        # Token budget for SDLC orchestrator messages. Larger than MAX_TOKENS
        # because SDLC is multi-round (supervisor→coder→tester→reviewer)
        # and each round appends to the shared message history.
        self.SDLC_MAX_TOKENS = int(os.getenv("SDLC_MAX_TOKENS", "16000"))
        # When true, prepare_sdlc_messages trims the conversation history to
        # SDLC_MAX_TOKENS using langchain's trim_messages (strategy="last"),
        # keeping the system prompt + most recent human/ai turns. Disabled
        # means messages grow unbounded (risk: context overflow on long tasks).
        self.ENABLE_SDLC_CONTEXT_TRIM = os.getenv("ENABLE_SDLC_CONTEXT_TRIM", "true").lower() == "true"
        # When true, the SDLC supervisor graph wraps each agent node with a
        # handoff tracer that logs structured "sdlc_handoff" events capturing
        # from_agent, to_agent, state_delta keys, and latency. This enables
        # debugging of multi-agent handoff failures (incomplete context
        # passed between agents).
        self.ENABLE_HANDOFF_TRACING = os.getenv("ENABLE_HANDOFF_TRACING", "true").lower() == "true"
        # MCP (Model Context Protocol) client support.
        # When enabled, the tool registry dynamically loads tools from remote
        # MCP servers listed in MCP_SERVER_URLS at startup.  Requires the
        # ``mcp`` and ``langchain-mcp-adapters`` packages (not in the default
        # dependency set — install on demand).  If the packages are missing,
        # MCP loading is skipped with a warning (non-blocking).
        self.ENABLE_MCP_CLIENT = os.getenv("ENABLE_MCP_CLIENT", "false").lower() == "true"
        # WebSocket support for real-time SDLC interaction (push status +
        # approve/reject via WS). Default false — SSE endpoints remain
        # the primary interface. Enable for real-time UI integrations.
        self.ENABLE_WEBSOCKET = os.getenv("ENABLE_WEBSOCKET", "false").lower() == "true"
        # Comma-separated list of MCP server URLs. All servers share the
        # same transport (configured by MCP_DEFAULT_TRANSPORT below).
        # See .env.example for format examples.
        self.MCP_SERVER_URLS = parse_list_from_env("MCP_SERVER_URLS", [])
        # MCP transport protocol — one of:
        #   "streamable_http" (default, MCP 2026 production standard;
        #     server URL form: http://host:port/mcp)
        #   "sse" (legacy Server-Sent-Events transport, deprecated;
        #     server URL form: http://host:port/sse)
        #   "stdio" (local subprocess; not yet wired through this entry
        #     point — use direct MultiServerMCPClient for stdio)
        # All servers in MCP_SERVER_URLS use the same transport. Mixed
        # transports require separate registry calls with explicit
        # ``transport=`` argument.
        self.MCP_DEFAULT_TRANSPORT = os.getenv("MCP_DEFAULT_TRANSPORT", "streamable_http").lower()
        self.MAX_LLM_CALL_RETRIES = int(os.getenv("MAX_LLM_CALL_RETRIES", "3"))
        self.LLM_TOTAL_TIMEOUT = int(os.getenv("LLM_TOTAL_TIMEOUT", "60"))
        # Outer request-budget timeout for non-streaming /chat. Bounds the
        # full graph.ainvoke (which may loop chat->tool->chat, making multiple
        # LLM calls). Must be > LLM_TOTAL_TIMEOUT so the inner per-call timeout
        # governs single calls while this bounds the aggregate.
        self.CHAT_NONSTREAM_TIMEOUT_SEC = int(os.getenv("CHAT_NONSTREAM_TIMEOUT_SEC", "120"))

        # Prompt version management — maps agent_name to active version.
        # Example env value: coder:v2,planner:v1
        # If an agent is not listed here, load_agent_prompt falls back to
        # the unversioned {agent_name}.md file (backward compatible).
        self.PROMPT_VERSIONS: dict[str, str] = self._parse_prompt_versions("PROMPT_VERSIONS")

        # SDLC Orchestrator Configuration
        self.USE_SDLC_ORCHESTRATOR = os.getenv("USE_SDLC_ORCHESTRATOR", "false").lower() == "true"
        self.SDLC_WORKSPACE_ROOT = Path(os.getenv("SDLC_WORKSPACE_ROOT", "workspace"))
        # Workspace preview — serve generated artifacts (HTML/games/dashboards)
        # over HTTP so the UI can render them in a sandboxed iframe. Toggling
        # this flag off disables both the StaticFiles mount and the asset
        # tools (see app/core/langgraph/tools/asset_tools.py).
        self.ENABLE_WORKSPACE_PREVIEW = os.getenv("ENABLE_WORKSPACE_PREVIEW", "true").lower() == "true"
        self.WORKSPACE_PREVIEW_URL_PREFIX = os.getenv("WORKSPACE_PREVIEW_URL_PREFIX", "/workspace")
        self.SDLC_MAX_FILE_SIZE = int(os.getenv("SDLC_MAX_FILE_SIZE", "10485760"))
        self.SDLC_ALLOWED_EXTENSIONS = parse_list_from_env(
            "SDLC_ALLOWED_EXTENSIONS",
            [
                # Code
                "py",
                "js",
                "ts",
                "jsx",
                "tsx",
                "json",
                "md",
                "txt",
                "yml",
                "yaml",
                "toml",
                # Web
                "html",
                "htm",
                "css",
                "xml",
                "svg",
                # Static assets
                "png",
                "jpg",
                "jpeg",
                "gif",
                "webp",
                "ico",
                "mp4",
                "webm",
                "mp3",
                "wav",
                "ogg",
                # Fonts
                "woff",
                "woff2",
                "ttf",
                "eot",
            ],
        )
        self.SDLC_GIT_ENABLED = os.getenv("SDLC_GIT_ENABLED", "true").lower() == "true"
        self.ENABLE_SECURITY_SCAN = os.getenv("ENABLE_SECURITY_SCAN", "true").lower() == "true"
        self.ENABLE_AUDIT_LOG = os.getenv("ENABLE_AUDIT_LOG", "true").lower() == "true"
        self.ENABLE_ADVANCED_MONITORING = os.getenv("ENABLE_ADVANCED_MONITORING", "true").lower() == "true"

        # Registration control: when false, /register returns 403.
        # New users are created via scripts/create_user.py (invite-only).
        self.ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "true").lower() == "true"

        # Long term memory Configuration
        self.LONG_TERM_MEMORY_MODEL = os.getenv("LONG_TERM_MEMORY_MODEL", "gpt-5-nano")
        self.LONG_TERM_MEMORY_EMBEDDER_MODEL = os.getenv("LONG_TERM_MEMORY_EMBEDDER_MODEL", "text-embedding-3-small")
        self.LONG_TERM_MEMORY_COLLECTION_NAME = os.getenv("LONG_TERM_MEMORY_COLLECTION_NAME", "longterm_memory")
        # Embedding dimension must match the embedder model's output dim.
        # Bailian text-embedding-v3 supports {64,128,256,512,768,1024};
        # default 1024 matches .env.production. OpenAI text-embedding-3-small
        # would use 1536 — adjust this if you switch providers.
        self.LONG_TERM_MEMORY_EMBEDDING_DIM = int(os.getenv("LONG_TERM_MEMORY_EMBEDDING_DIM", "1024"))

        # Code Index Configuration
        self.CODE_INDEX_ENABLED = os.getenv("CODE_INDEX_ENABLED", "true").lower() == "true"
        self.CODE_INDEX_COLLECTION_NAME = os.getenv("CODE_INDEX_COLLECTION_NAME", "code_index")
        self.CODE_INDEX_CHUNK_SIZE = int(os.getenv("CODE_INDEX_CHUNK_SIZE", "100"))
        self.CODE_INDEX_CHUNK_OVERLAP = int(os.getenv("CODE_INDEX_CHUNK_OVERLAP", "20"))
        self.CODE_INDEX_STRUCTURAL_PARSING = os.getenv("CODE_INDEX_STRUCTURAL_PARSING", "true").lower() == "true"
        self.CODE_INDEX_MAX_FILE_SIZE = int(os.getenv("CODE_INDEX_MAX_FILE_SIZE", "102400"))

        # Knowledge Base (RAG) Configuration — user-uploaded documents
        # (PDF, Markdown, API specs) indexed for semantic search.
        # Per-user isolation (user_id=str(user_id)), shared across all
        # tasks. Reuses pgvector + mem0 infra with a separate collection.
        # Opt-in via ENABLE_KNOWLEDGE_BASE (default false).
        self.ENABLE_KNOWLEDGE_BASE = os.getenv("ENABLE_KNOWLEDGE_BASE", "false").lower() == "true"
        self.KNOWLEDGE_BASE_COLLECTION_NAME = os.getenv("KNOWLEDGE_BASE_COLLECTION_NAME", "knowledge_base")
        self.KB_MAX_FILE_SIZE = int(os.getenv("KB_MAX_FILE_SIZE", "10485760"))
        self.KB_ALLOWED_EXTENSIONS = parse_list_from_env(
            "KB_ALLOWED_EXTENSIONS", ["md", "txt", "pdf", "json", "yaml", "yml"]
        )
        self.KB_CHUNK_SIZE = int(os.getenv("KB_CHUNK_SIZE", "500"))
        self.KB_CHUNK_OVERLAP = int(os.getenv("KB_CHUNK_OVERLAP", "50"))
        self.KB_MAX_RESULTS = int(os.getenv("KB_MAX_RESULTS", "5"))

        # Real-time market quotes — chat agent's fetch_realtime_quote tool.
        # Pulls live data from Tencent Finance (qt.gtimg.cn) on demand.
        # Distinct from knowledge base snapshots: KB answers "what did the
        # market look like at upload time", this answers "what is the price
        # right now". Default true (low cost, no auth, no rate-limit issues
        # for individual use).
        self.ENABLE_REALTIME_QUOTES = os.getenv("ENABLE_REALTIME_QUOTES", "true").lower() == "true"
        # DuckDuckGo web search tool for the chat agent. Disable when the
        # container network cannot reach external HTTPS endpoints (TLS
        # handshake failures cause 20s+ timeouts and 502 gateway errors).
        self.ENABLE_WEB_SEARCH = os.getenv("ENABLE_WEB_SEARCH", "true").lower() == "true"
        # Comma-separated whitelist of allowed symbol prefixes. Restricts
        # the LLM from probing arbitrary tickers. A-share: sh/sz, HK: hk,
        # US: us, commodity: hf_, index: 1/2/0-prefix codes.
        self.QUOTE_ALLOWED_PREFIXES = parse_list_from_env("QUOTE_ALLOWED_PREFIXES", ["sh", "sz", "hk", "us", "hf_"])
        # Max symbols per single tool call. Prevents the LLM from
        # triggering 50+ HTTP requests in one go.
        self.QUOTE_MAX_SYMBOLS_PER_CALL = int(os.getenv("QUOTE_MAX_SYMBOLS_PER_CALL", "20"))
        # HTTP timeout per request in seconds. Tencent Finance is
        # typically < 500ms; 8s is the same budget used in
        # test_data/generate_market_snapshot.py.
        self.QUOTE_HTTP_TIMEOUT_SECONDS = int(os.getenv("QUOTE_HTTP_TIMEOUT_SECONDS", "8"))
        # Retry count for transient network errors
        # (RemoteDisconnected, ConnectionReset).
        self.QUOTE_HTTP_RETRIES = int(os.getenv("QUOTE_HTTP_RETRIES", "3"))

        # vLLM health monitor — periodic scrape of the local vLLM
        # Prometheus endpoint to surface OOM-risk signals
        # (num_preemptions_total, gpu_cache_usage_perc). See  # noqa: ERA001
        # app/core/vllm_health.py for details. Default on because
        # monitoring cost is ~1 HTTP req per interval and vLLM is
        # optional, so no failure mode is destructive.
        self.VLLM_HEALTH_CHECK_ENABLED = os.getenv("VLLM_HEALTH_CHECK_ENABLED", "true").lower() == "true"
        # Metrics endpoint URL. In compose the hostname is the service
        # name; outside compose use http://host.docker.internal:8001/metrics
        self.VLLM_METRICS_URL = os.getenv("VLLM_METRICS_URL", "http://vllm:8001/metrics")
        # Polling interval. 30s is the same cadence as the vllm_metrics
        # --watch loop and the docker healthcheck; cheap to run.
        self.VLLM_HEALTH_CHECK_INTERVAL_SECONDS = int(os.getenv("VLLM_HEALTH_CHECK_INTERVAL_SECONDS", "30"))
        # Per-request timeout. The /metrics endpoint is local and
        # lightweight; 5s is generous.
        self.VLLM_HEALTH_CHECK_TIMEOUT_SECONDS = int(os.getenv("VLLM_HEALTH_CHECK_TIMEOUT_SECONDS", "5"))
        # Any non-zero preemption delta triggers a WARNING event.
        # Raise to 5+ if running stress tests to avoid log noise.
        self.VLLM_PREEMPTION_WARN_THRESHOLD = int(os.getenv("VLLM_PREEMPTION_WARN_THRESHOLD", "1"))
        # KV cache % above which we emit a warning. 0.85 is the
        # threshold below which vLLM has comfortable headroom on the
        # 4GB-VRAM single-user box.
        self.VLLM_KV_CACHE_WARN_THRESHOLD = float(os.getenv("VLLM_KV_CACHE_WARN_THRESHOLD", "0.85"))
        # KV cache % above which we emit an ERROR — close to OOM if a
        # long-context request arrives. 0.95 mirrors the Grafana panel
        # in grafana/dashboards/json/vllm_local.json.
        self.VLLM_KV_CACHE_CRITICAL_THRESHOLD = float(os.getenv("VLLM_KV_CACHE_CRITICAL_THRESHOLD", "0.95"))

        self.ENABLE_KNOWLEDGE_GRAPH = os.getenv("ENABLE_KNOWLEDGE_GRAPH", "true").lower() == "true"
        self.KNOWLEDGE_GRAPH_MAX_NODES = int(os.getenv("KNOWLEDGE_GRAPH_MAX_NODES", "1000"))
        self.KNOWLEDGE_GRAPH_MAX_DEPTH = int(os.getenv("KNOWLEDGE_GRAPH_MAX_DEPTH", "5"))

        self.ENABLE_SMART_SEARCH = os.getenv("ENABLE_SMART_SEARCH", "true").lower() == "true"
        self.SMART_SEARCH_CACHE_TTL_SECONDS = int(os.getenv("SMART_SEARCH_CACHE_TTL_SECONDS", "300"))
        self.SMART_SEARCH_MAX_RESULTS = int(os.getenv("SMART_SEARCH_MAX_RESULTS", "15"))

        self.ENABLE_SMART_CODER = os.getenv("ENABLE_SMART_CODER", "true").lower() == "true"

        # Multimodal Configuration
        self.ENABLE_MULTIMODAL = os.getenv("ENABLE_MULTIMODAL", "false").lower() == "true"
        self.MAX_IMAGE_SIZE_MB = int(os.getenv("MAX_IMAGE_SIZE_MB", "5"))
        self.ALLOWED_IMAGE_TYPES = parse_list_from_env("ALLOWED_IMAGE_TYPES", ["png", "jpg", "jpeg", "webp"])
        self.VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")
        # Max tokens for a single chat vision analysis (Route B). Kept small:
        # we only need a concise describe-only summary for chat context.
        self.VISION_MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "1024"))

        # Image generation (DashScope native API, NOT OpenAI-compatible).
        # Models: wan2.6-t2i (text-to-image), wan2.6-imageedit (image edit).
        # Free quota: ~500 images, valid 90 days from Bailian activation.
        self.IMAGE_GENERATION_MODEL = os.getenv("IMAGE_GENERATION_MODEL", "wan2.6-t2i")
        self.IMAGE_GENERATION_SIZE = os.getenv("IMAGE_GENERATION_SIZE", "1024*1024")
        self.IMAGE_GENERATION_COUNT = int(os.getenv("IMAGE_GENERATION_COUNT", "1"))

        # Video generation (DashScope native API, async).
        # Models: wan2.7-t2v (latest T2V), wan2.6-t2v, wan2.6-i2v (image-to-video).
        # Free quota: 50 seconds, valid 90 days.
        self.VIDEO_GENERATION_MODEL = os.getenv("VIDEO_GENERATION_MODEL", "wan2.7-t2v")
        self.VIDEO_GENERATION_RESOLUTION = os.getenv("VIDEO_GENERATION_RESOLUTION", "720P")
        self.VIDEO_GENERATION_DURATION = int(os.getenv("VIDEO_GENERATION_DURATION", "5"))

        # M-12 fix: max concurrent video generations per user. Video polling
        # blocks 1-5 min; without a cap a single user could tie up many
        # event-loop slots. Default 1 (one video at a time per user).
        self.MAX_CONCURRENT_VIDEO_PER_USER = int(os.getenv("MAX_CONCURRENT_VIDEO_PER_USER", "1"))
        # M-13 fix: daily media generation cap per user (images + videos
        # combined). Protects the shared account-level DashScope free quota
        # from a single user exhausting it. In-memory counter, resets on
        # restart. Default 20.
        self.MAX_MEDIA_GENERATIONS_PER_USER_PER_DAY = int(os.getenv("MAX_MEDIA_GENERATIONS_PER_USER_PER_DAY", "20"))

        # Chat vision analysis (Route B): daily per-user cap for image analysis
        # in chat, independent of the SDLC media-generation quota above.
        self.MAX_VISION_ANALYSES_PER_USER_PER_DAY = int(os.getenv("MAX_VISION_ANALYSES_PER_USER_PER_DAY", "30"))

        # Resource Quota Configuration
        self.ENABLE_RESOURCE_QUOTA = os.getenv("ENABLE_RESOURCE_QUOTA", "true").lower() == "true"
        self.MAX_WORKSPACE_SIZE_MB = int(os.getenv("MAX_WORKSPACE_SIZE_MB", "500"))
        self.MAX_IMAGES_PER_TASK = int(os.getenv("MAX_IMAGES_PER_TASK", "5"))
        self.MAX_TASKS_PER_USER = int(os.getenv("MAX_TASKS_PER_USER", "20"))
        self.IMAGE_ANALYSIS_CACHE_TTL_SECONDS = int(os.getenv("IMAGE_ANALYSIS_CACHE_TTL_SECONDS", "600"))

        # Script Execution Configuration
        self.ENABLE_SCRIPT_EXECUTION = os.getenv("ENABLE_SCRIPT_EXECUTION", "true").lower() == "true"
        self.SCRIPT_TIMEOUT_SECONDS = int(os.getenv("SCRIPT_TIMEOUT_SECONDS", "60"))
        self.SCRIPT_MAX_RETRIES = int(os.getenv("SCRIPT_MAX_RETRIES", "3"))

        # Smart Editing Configuration
        self.ENABLE_SMART_EDITING = os.getenv("ENABLE_SMART_EDITING", "true").lower() == "true"
        self.SMART_EDIT_MAX_FILES = int(os.getenv("SMART_EDIT_MAX_FILES", "10"))
        self.SMART_EDIT_AUTO_APPROVE_THRESHOLD = int(os.getenv("SMART_EDIT_AUTO_APPROVE_THRESHOLD", "3"))

        # Post-edit Validation Configuration
        self.ENABLE_POST_EDIT_VALIDATION = os.getenv("ENABLE_POST_EDIT_VALIDATION", "true").lower() == "true"
        self.VALIDATION_TIMEOUT_SECONDS = int(os.getenv("VALIDATION_TIMEOUT_SECONDS", "30"))
        self.MAX_CODER_RETRIES = int(os.getenv("MAX_CODER_RETRIES", "3"))

        # Sandbox Configuration
        # Controls how untrusted code (run_tests, generate_and_run_script) is executed.
        # "docker" = Docker container isolation (recommended for production).
        # "disabled" = tools return "unavailable" error (safe, no execution).
        # IMPORTANT: never set to "host" — that would re-enable bare subprocess execution.
        self.SANDBOX_BACKEND = os.getenv("SANDBOX_BACKEND", "docker").lower()
        self.SANDBOX_DOCKER_IMAGE = os.getenv("SANDBOX_DOCKER_IMAGE", "python:3.13-slim")
        self.SANDBOX_MEMORY_LIMIT = os.getenv("SANDBOX_MEMORY_LIMIT", "512m")
        self.SANDBOX_CPU_LIMIT = os.getenv("SANDBOX_CPU_LIMIT", "1.0")

        # JWT Configuration
        self.JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
        self.JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
        self.JWT_ACCESS_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_DAYS", "30"))

        # Logging Configuration
        self.LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        self.LOG_FORMAT = os.getenv("LOG_FORMAT", "json")  # "json" or "console"
        # 日志保留天数：超过此天数的日志文件会在启动时清理。0 = 禁用。
        self.LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))

        # Profiling Configuration (DEBUG only)
        self.PROFILING_DIR = Path(os.getenv("PROFILING_DIR", os.path.join(tempfile.gettempdir(), "fastapi_profiles")))
        self.PROFILING_THRESHOLD_SECONDS = float(os.getenv("PROFILING_THRESHOLD_SECONDS", "2.0"))

        # Postgres Configuration
        self.POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
        self.POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
        self.POSTGRES_DB = os.getenv("POSTGRES_DB", "food_order_db")
        self.POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
        self.POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
        self.POSTGRES_POOL_SIZE = int(os.getenv("POSTGRES_POOL_SIZE", "20"))
        self.POSTGRES_MAX_OVERFLOW = int(os.getenv("POSTGRES_MAX_OVERFLOW", "10"))
        self.CHECKPOINT_TABLES = ["checkpoint_blobs", "checkpoint_writes", "checkpoints"]
        # Feature Flag: when true, business DB operations route through the
        # async SQLAlchemy 2.x layer (app/core/db/session.py) instead of the
        # legacy sync SQLModel engine + run_in_thread dispatch. Defaults to
        # false so the migration is opt-in per environment.
        self.ENABLE_ASYNC_DB = os.getenv("ENABLE_ASYNC_DB", "false").lower() == "true"

        # Valkey/Redis Cache Configuration (optional — if host is set, caching is enabled)
        self.VALKEY_HOST = os.getenv("VALKEY_HOST", "")
        self.VALKEY_PORT = int(os.getenv("VALKEY_PORT", "6379"))
        self.VALKEY_DB = int(os.getenv("VALKEY_DB", "0"))
        self.VALKEY_PASSWORD = os.getenv("VALKEY_PASSWORD", "")
        self.VALKEY_MAX_CONNECTIONS = int(os.getenv("VALKEY_MAX_CONNECTIONS", "20"))
        self.CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))

        # Semantic Cache Configuration (pgvector-based, embedding similarity)
        self.SEMANTIC_CACHE_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() == "true"
        self.SEMANTIC_CACHE_EMBEDDING_MODEL = os.getenv("SEMANTIC_CACHE_EMBEDDING_MODEL", "text-embedding-v3")
        self.SEMANTIC_CACHE_MEMORY_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_MEMORY_THRESHOLD", "0.90"))
        self.SEMANTIC_CACHE_LLM_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_LLM_THRESHOLD", "0.95"))
        self.SEMANTIC_CACHE_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "3600"))

        # Rate Limiting Configuration
        self.RATE_LIMIT_DEFAULT = parse_list_from_env("RATE_LIMIT_DEFAULT", ["200 per day", "50 per hour"])

        # Rate limit endpoints defaults
        default_endpoints = {
            "chat": ["30 per minute"],
            "chat_stream": ["20 per minute"],
            "messages": ["50 per minute"],
            "register": ["10 per hour"],
            "login": ["20 per minute"],
            "session": ["20 per hour"],
            "root": ["10 per minute"],
            "health": ["20 per minute"],
        }

        # Update rate limit endpoints from environment variables
        self.RATE_LIMIT_ENDPOINTS = default_endpoints.copy()
        for endpoint in default_endpoints:
            env_key = f"RATE_LIMIT_{endpoint.upper()}"
            value = parse_list_from_env(env_key)
            if value:
                self.RATE_LIMIT_ENDPOINTS[endpoint] = value

        # Evaluation Configuration
        self.EVALUATION_LLM = os.getenv("EVALUATION_LLM", "gpt-5")
        self.EVALUATION_BASE_URL = os.getenv("EVALUATION_BASE_URL", "https://api.openai.com/v1")
        self.EVALUATION_API_KEY = os.getenv("EVALUATION_API_KEY", self.OPENAI_API_KEY)
        self.EVALUATION_SLEEP_TIME = int(os.getenv("EVALUATION_SLEEP_TIME", "10"))

        # Apply environment-specific settings
        self.apply_environment_settings()

        # Validate security-sensitive settings (raises ValueError on violation)
        self.validate_security()

    def _parse_prompt_versions(self, env_key: str) -> dict[str, str]:
        """Parse PROMPT_VERSIONS env var into a dict.

        Format: "coder:v2,planner:v1" → {"coder": "v2", "planner": "v1"}
        Empty or unset → empty dict (fall back to unversioned prompts).
        """
        raw = os.getenv(env_key, "").strip()
        if not raw:
            return {}
        result: dict[str, str] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            agent, version = pair.split(":", 1)
            agent = agent.strip()
            version = version.strip()
            if agent and version:
                result[agent] = version
        return result

    def apply_environment_settings(self):
        """Apply environment-specific settings based on the current environment."""
        env_settings = {
            Environment.DEVELOPMENT: {
                "DEBUG": True,
                "LOG_LEVEL": "DEBUG",
                "LOG_FORMAT": "console",
                "RATE_LIMIT_DEFAULT": ["1000 per day", "200 per hour"],
            },
            Environment.STAGING: {
                "DEBUG": False,
                "LOG_LEVEL": "INFO",
                "RATE_LIMIT_DEFAULT": ["500 per day", "100 per hour"],
            },
            Environment.PRODUCTION: {
                "DEBUG": False,
                "LOG_LEVEL": "WARNING",
                "RATE_LIMIT_DEFAULT": ["200 per day", "50 per hour"],
            },
            Environment.TEST: {
                "DEBUG": True,
                "LOG_LEVEL": "DEBUG",
                "LOG_FORMAT": "console",
                "RATE_LIMIT_DEFAULT": ["1000 per day", "1000 per hour"],  # Relaxed for testing
            },
        }

        # Get settings for current environment
        current_env_settings = env_settings.get(self.ENVIRONMENT, {})

        # Apply settings if not explicitly set in environment variables
        for key, value in current_env_settings.items():
            env_var_name = key.upper()
            # Only override if environment variable wasn't explicitly set
            if env_var_name not in os.environ:
                setattr(self, key, value)

    def validate_security(self) -> None:
        """Validate security-sensitive settings at startup.

        Three tiers of checks:

        1. **All environments — type/range** (``POSTGRES_PORT``,
           ``JWT_ALGORITHM``, ``SANDBOX_BACKEND``, ``LOG_FORMAT``).
           Catches typos that would otherwise fail silently at runtime.

        2. **All environments — security** — ``SANDBOX_BACKEND`` must
           never be ``"host"`` (would re-enable bare subprocess
           execution, bypassing sandbox isolation).

        3. **Production & Staging only** — JWT secret strength, DEBUG
           flag, API key presence. Development/Test environments are
           exempt so local workflows are not blocked.

        Uses ``raise ValueError`` (not ``assert``) so checks survive
        ``python -O`` optimisation mode.

        Raises:
            ValueError: If any violation is detected. The message lists
                all violations (not just the first) so the operator can
                fix everything in one pass.
        """
        violations: list[str] = []

        # --- Tier 1: type / range (all environments) ----------------------
        if not (1 <= self.POSTGRES_PORT <= 65535):
            violations.append(f"POSTGRES_PORT must be in 1..65535 (got {self.POSTGRES_PORT})")

        _valid_jwt_algorithms = {
            "HS256",
            "HS384",
            "HS512",
            "RS256",
            "RS384",
            "RS512",
            "ES256",
            "ES384",
            "ES512",
            "PS256",
            "PS384",
            "PS512",
            "EdDSA",
        }
        if self.JWT_ALGORITHM not in _valid_jwt_algorithms:
            violations.append(
                f"JWT_ALGORITHM={self.JWT_ALGORITHM!r} is not a recognised "
                f"algorithm. Valid: {sorted(_valid_jwt_algorithms)}"
            )

        if self.SANDBOX_BACKEND not in ("docker", "disabled"):
            violations.append(
                f"SANDBOX_BACKEND={self.SANDBOX_BACKEND!r} is invalid. Must be 'docker' or 'disabled' (never 'host')."
            )

        if self.LOG_FORMAT not in ("json", "console"):
            violations.append(f"LOG_FORMAT={self.LOG_FORMAT!r} is invalid. Must be 'json' or 'console'.")

        # --- Tier 2: security (all environments) --------------------------
        # ``SANDBOX_BACKEND == 'host'`` is already rejected by Tier 1's
        # enum check above; this explicit guard documents *why* and keeps
        # the error message actionable if someone adds new values later.
        if self.SANDBOX_BACKEND == "host":
            violations.append(
                "SANDBOX_BACKEND='host' is forbidden — enables bare subprocess "
                "execution (security risk). Use 'docker' or 'disabled'."
            )

        # --- Tier 3: production & staging only -----------------------------
        if self.ENVIRONMENT in (Environment.PRODUCTION, Environment.STAGING):
            if len(self.JWT_SECRET_KEY) < 32:
                violations.append(
                    f"JWT_SECRET_KEY must be >= 32 chars in {self.ENVIRONMENT.value} "
                    f"(got {len(self.JWT_SECRET_KEY)}). Generate with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )

            if self.DEBUG:
                violations.append(f"DEBUG must be False in {self.ENVIRONMENT.value} environment")

            if not self.OPENAI_API_KEY:
                violations.append(
                    f"OPENAI_API_KEY must be set in {self.ENVIRONMENT.value} (LLM service unavailable without it)"
                )

            # DEFAULT_LLM_MODEL: empty default removed; production/staging
            # must set it explicitly so model routing never silently falls
            # back to a non-existent model name. Mirrors OPENAI_API_KEY guard.
            if not self.DEFAULT_LLM_MODEL:
                violations.append(
                    f"DEFAULT_LLM_MODEL must be set in {self.ENVIRONMENT.value} "
                    f"(empty default removed to force explicit configuration)"
                )

        if violations:
            raise ValueError(
                f"Configuration validation failed for {self.ENVIRONMENT.value} environment:\n"
                + "\n".join(f"  - {v}" for v in violations)
            )


# Create settings instance
settings = Settings()
