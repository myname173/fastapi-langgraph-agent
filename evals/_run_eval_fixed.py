import os
import sys
import asyncio

# Ensure project root is importable (the module lives in evals/ and imports the
# top-level package via `from evals.sdlc_evals import ...` / `app.*`).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load local API key from secrets file (gitignored). Never hardcode in source.
_SECRETS = os.path.join(os.path.dirname(__file__), "..", "secrets", "openai_api_key.txt")
if os.path.exists(_SECRETS):
    with open(_SECRETS, "r", encoding="utf-8") as fh:
        _key = fh.read().strip()
    os.environ["OPENAI_API_KEY"] = _key
    os.environ["EVALUATION_API_KEY"] = _key
    os.environ["DASHSCOPE_API_KEY"] = _key
    print(f"KEY_LOADED length={len(_key)}", flush=True)
else:
    print("KEY_FILE_MISSING", flush=True)
    sys.exit(1)

# Local eval uses MemorySaver instead of Postgres checkpointer.
os.environ.setdefault("SDLC_EVAL_LOCAL", "1")
os.environ["APP_ENV"] = "production"

# Local eval has NO Postgres. Disable PG-backed features so they fail fast
# (graceful degradation) instead of retrying for 30s+ on every LLM call,
# which otherwise blows past LLM_TOTAL_TIMEOUT and slows the whole suite.
os.environ["SEMANTIC_CACHE_ENABLED"] = "false"
os.environ["ENABLE_ASYNC_DB"] = "false"
# Langfuse host (jp.cloud.langfuse.com) is unreachable from this local env and
# its OTel exporter blocks on 5s socket timeouts, spamming tracebacks and
# slowing the run. Disable tracing for the local eval. (AGENTS.md mandates
# tracing in production; this only affects the offline eval harness.)
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
# Knowledge base (mem0 / pgvector) needs Postgres; opt out to avoid 30s
# connection-timeout storms on every coder step.
os.environ["ENABLE_KNOWLEDGE_BASE"] = "false"
# Code-index (pgvector semantic search) also needs Postgres and is not part of
# the SDLC task correctness; disable so coder steps don't block on PG timeouts.
os.environ["CODE_INDEX_ENABLED"] = "false"
# Long-term memory (mem0) needs Postgres too; keep it off for the offline eval.
os.environ["ENABLE_FINANCIAL_MEMORY"] = "false"
# Hard-disable the mem0/pgvector memory service so search()/add() short-circuit
# instead of each burning a 30s pgvector connection timeout (no Postgres here).
os.environ["MEMORY_SERVICE_ENABLED"] = "false"

# Model selection for the offline eval suite.
#
# HISTORY: this file used to hard-code the Bailian models that happened to be
# alive on 2026-08-12 (qwen3.7-flash / qwen3.7-max-preview). Both are gone now —
# the chat provider moved to DeepSeek (see .env.production CHAT_BASE_URL), and
# the hard-coded ids meant the harness could no longer run at all.
#
# Keep the defaults aligned with .env.production's LLM_MODELS, and override per
# run without editing this file:
#   SDLC_EVAL_MODEL=deepseek-flash SDLC_EVAL_MODEL_STRONG=deepseek-v4-pro \
#       python evals/_run_eval_fixed.py
# The per-test map is consumed by sdlc_evals.run_all_tests, which switches the
# active model (and rebuilds the registry/router fallback) before each test.
_MODEL_DEFAULT = os.getenv("SDLC_EVAL_MODEL", "deepseek-flash")
_MODEL_STRONG = os.getenv("SDLC_EVAL_MODEL_STRONG", "deepseek-v4-pro")
import json as _json  # noqa: E402

_STRONG_OVERRIDES = _json.loads(os.getenv("LLM_PROVIDER_OVERRIDES", "") or "{}")
os.environ["LLM_PROVIDER_OVERRIDES"] = _json.dumps(_STRONG_OVERRIDES)
os.environ["DEFAULT_LLM_MODEL"] = _MODEL_DEFAULT
os.environ["EVALUATION_LLM"] = _MODEL_DEFAULT
os.environ["LLM_MODELS"] = _MODEL_STRONG  # fallback chain honours the strong one too
os.environ["CODER_ELIGIBLE_MODELS"] = _MODEL_STRONG
os.environ["CHAT_ROTATION_MODELS"] = _MODEL_DEFAULT
os.environ["SDLC_EVAL_DEFAULT_MODEL"] = _MODEL_DEFAULT
_TEST_MODEL_MAP = {
    "bug_fix_off_by_one": _MODEL_STRONG,
    "feature_add_ttl_cache": _MODEL_STRONG,
    "api_migration_requests_to_httpx": _MODEL_STRONG,
    "security_repair_sqli": _MODEL_STRONG,
    "multi_file_refactor_split": _MODEL_STRONG,
    "cli_script_fibonacci": _MODEL_DEFAULT,
    "test_generation_coverage": _MODEL_DEFAULT,
}
os.environ["SDLC_EVAL_TEST_MODEL_MAP"] = __import__("json").dumps(_TEST_MODEL_MAP)

from evals.sdlc_evals import SDLCEvaluator  # noqa: E402

# Initialise the registry/router fallback to the default model so the very
# first test (and any test not in the map) starts cleanly. run_all_tests
# re-switches per test from SDLC_EVAL_TEST_MODEL_MAP.
import structlog as _structlog  # noqa: E402

_LOGGER = _structlog.get_logger("eval_model_override")
try:
    from app.services.llm import router as _router
    from app.services.llm import registry as _registry

    for _role in list(_router.ROUTING_TABLE.keys()):
        _router.ROUTING_TABLE[_role] = _MODEL_DEFAULT
    _router.model_router.ROUTING_TABLE = _router.ROUTING_TABLE
    _default_llm = _registry._build_llm(_MODEL_DEFAULT)
    _registry.LLMRegistry.LLMS = [{"name": _MODEL_DEFAULT, "llm": _default_llm}]
    _LOGGER.warning("eval_model_override_applied", default=_MODEL_DEFAULT, strong=_MODEL_STRONG)
except Exception as _override_err:  # noqa: BLE001 - never hard-fail the harness
    _LOGGER.exception("eval_model_override_failed", error=str(_override_err))


async def _main():
    evaluator = SDLCEvaluator()
    result = await evaluator.run_all_tests()
    out_path = result.get("output_path")
    print(f"EVAL_DONE output={out_path}", flush=True)
    passed = result["summary"]["passed"]
    total = result["summary"]["total"]
    print(f"PASS_RATE={result['summary'].get('pass_rate')} passed={passed}/{total}", flush=True)


if __name__ == "__main__":
    # NOTE: On Windows, subprocess execution (used by the SDLC sandbox
    # `run_tests` / `run_script` fallback) requires the Proactor event loop.
    # SelectorEventLoop on Win32 raises NotImplementedError from
    # `_make_subprocess_transport`, which surfaced as an empty-str exception in
    # the sandbox fallback. Use ProactorEventLoop on Windows; elsewhere the
    # default policy is fine.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop_factory = asyncio.ProactorEventLoop
    else:
        loop_factory = asyncio.SelectorEventLoop
    asyncio.run(_main(), loop_factory=loop_factory)
