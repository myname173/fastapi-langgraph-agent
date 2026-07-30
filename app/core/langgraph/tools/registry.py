"""Declarative tool registry with agent-scoped and feature-flag-gated lookups.

Replaces the scattered ``{tool.name: tool for tool in tools}`` dicts and
hard-coded string-key lookups that previously lived inside ``SupervisorGraph``
and ``LangGraphAgent``.

Design principles
-----------------

1. **Register once, query many.**  Tools are registered at import time with
   declarative metadata (which agents may use them, semantic tags, and
   feature-flag requirements).  ``get_for_agent`` / ``get_by_name`` are the
   only access patterns.

2. **Dynamic feature-flag filtering.**  Feature flags (``ENABLE_MULTIMODAL``
   etc.) are evaluated at *query* time against the live ``settings`` object,
   not baked in at registration.  This means toggling a flag takes effect
   immediately without rebuilding the registry — essential for tests that
   ``patch.object(settings, ...)`` per-fixture (see ``conftest.py``).

3. **Two access patterns preserved:**
   - ``get_for_agent(name)`` → LLM-bound tool list (for ``bind_tools``).
   - ``get_by_name(name)`` → single tool for programmatic invocation
     (e.g. ``validate_changes`` called directly inside ``_coder_node``).

4. **Backward compatible.**  ``get_by_name`` is a drop-in for the old
   ``sdlc_tools_by_name["..."]`` pattern.
"""

from dataclasses import dataclass

from langchain_core.tools.base import BaseTool

from app.core.config import settings
from app.core.logging import logger


@dataclass(frozen=True)
class RegisteredTool:
    """A tool plus its declarative metadata.

    Attributes:
        tool: The LangChain tool instance.
        agents: Agent names that may bind this tool to their LLM
            (e.g. ``{"coder", "tester"}``).  Empty set means the tool is
            registered for programmatic access only (via ``get_by_name``).
        tags: Semantic tags for grouping (e.g. ``{"file_io"}``).
        requires: Feature-flag attribute names on ``settings`` that must be
            truthy for the tool to be included in ``get_for_agent`` results.
            Evaluated dynamically at query time.
    """

    tool: BaseTool
    agents: frozenset[str]
    tags: frozenset[str]
    requires: frozenset[str]


class ToolRegistry:
    """Central registry for LangGraph tools.

    Tools are registered once (at import time) with declarative metadata.
    Lookups apply feature-flag filtering dynamically so toggling a setting
    takes effect without rebuilding the registry.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        tool: BaseTool,
        *,
        agents: set[str] | None = None,
        tags: set[str] | None = None,
        requires: set[str] | None = None,
    ) -> None:
        """Register a tool with declarative metadata.

        Args:
            tool: The LangChain tool to register.
            agents: Agent names allowed to bind this tool.  ``None`` or empty
                means programmatic-access-only.
            tags: Semantic tags for grouping/filtering.
            requires: Feature-flag attribute names on ``settings`` that must
                be truthy for ``get_for_agent`` inclusion.
        """
        if tool.name in self._tools:
            logger.warning("tool_already_registered_overwriting", tool_name=tool.name)
        self._tools[tool.name] = RegisteredTool(
            tool=tool,
            agents=frozenset(agents or set()),
            tags=frozenset(tags or set()),
            requires=frozenset(requires or set()),
        )

    def get_by_name(self, name: str) -> BaseTool:
        """Retrieve a single tool by name (programmatic access).

        Does NOT apply feature-flag filtering — the caller decides whether
        the flag is relevant.  Mirrors the legacy ``sdlc_tools_by_name[name]``
        pattern.

        Raises:
            KeyError: If no tool with ``name`` is registered.
        """
        return self._tools[name].tool

    def has(self, name: str) -> bool:
        """Check whether a tool is registered (before ``get_by_name``)."""
        return name in self._tools

    def get_for_agent(self, agent_name: str) -> list[BaseTool]:
        """Return the LLM-bound tool list for an agent.

        Filters by agent membership AND dynamic feature-flag requirements.
        Order follows registration order (stable).
        """
        result: list[BaseTool] = []
        for entry in self._tools.values():
            if agent_name not in entry.agents:
                continue
            if not self._requirements_met(entry.requires):
                continue
            result.append(entry.tool)
        return result

    def all_names(self) -> list[str]:
        """Return all registered tool names (for diagnostics)."""
        return list(self._tools.keys())

    async def register_mcp_server(
        self,
        url: str,
        transport: str | None = None,
    ) -> list[str]:
        """Load tools from a remote MCP server and register them.

        Uses ``langchain-mcp-adapters`` to convert MCP tools into LangChain
        ``BaseTool`` instances, then registers each with the ``mcp`` tag.

        Transport is configuration-driven (no hard-coding):
          * ``streamable_http`` (default, MCP 2026 production standard) —
            URL form ``http://host:port/mcp``
          * ``sse`` (legacy, deprecated) — URL form ``http://host:port/sse``
          * ``stdio`` (local subprocess) — not wired through this entry
            point; caller should use ``MultiServerMCPClient`` directly
            because stdio requires ``command``/``args`` fields, not a URL.

        This method is **non-fatal**: if the ``mcp`` or
        ``langchain-mcp-adapters`` packages are not installed, or the server
        is unreachable, or the transport is unsupported, it logs a warning
        and returns an empty list.  This keeps the application startup
        resilient — MCP is an opt-in enhancement, not a hard dependency.

        Args:
            url: MCP server URL (e.g. ``"http://localhost:8002/mcp"``).
            transport: MCP transport protocol.  ``None`` reads from
                ``settings.MCP_DEFAULT_TRANSPORT`` (default
                ``"streamable_http"``).  Supported values: ``"streamable_http"``,
                ``"sse"``.

        Returns:
            List of tool names successfully registered from this server.
        """
        if transport is None:
            transport = settings.MCP_DEFAULT_TRANSPORT

        # stdio requires command/args (not a URL) — gracefully skip here.
        if transport == "stdio":
            logger.warning(
                "mcp_stdio_transport_not_supported_via_url",
                url=url,
                hint="Use MultiServerMCPClient directly for stdio transport",
            )
            return []

        try:
            # Lazy import — avoids hard dependency on mcp packages.
            from langchain_mcp_adapters.client import MultiServerMCPClient  # pyright: ignore[reportMissingImports]
        except ImportError:
            logger.warning(
                "mcp_packages_not_installed_skipping",
                url=url,
                hint="Install with: uv pip install mcp langchain-mcp-adapters",
            )
            return []

        try:
            # Build client config — transport key matches MultiServerMCPClient
            # expectations (streamable_http / sse). URL is opaque to us; the
            # server's mount path (e.g. /mcp vs /sse) is the operator's choice.
            # langchain-mcp-adapters types the ctor as dict[str, Connection],
            # but the runtime accepts a nested dict config (url+transport)
            # per server. This is the documented shape in the library
            # README; the stub is too strict.
            client = MultiServerMCPClient(
                {  # pyright: ignore[reportArgumentType]
                    "mcp_server": {
                        "url": url,
                        "transport": transport,
                    }
                }
            )
            mcp_tools = await client.get_tools()

            registered: list[str] = []
            for tool in mcp_tools:
                # Prefix with "mcp_" to avoid name collisions with builtins.
                tool.name = f"mcp_{tool.name}"
                self.register(
                    tool,
                    agents={"coder", "tester"},  # MCP tools available to SDLC agents
                    tags={"mcp", "external"},
                )
                registered.append(tool.name)

            logger.info(
                "mcp_server_loaded",
                url=url,
                transport=transport,
                tool_count=len(registered),
                tools=registered,
            )
            return registered

        except Exception as e:
            logger.exception(
                "mcp_server_load_failed",
                url=url,
                transport=transport,
                error=str(e),
            )
            return []

    async def load_mcp_servers(self, urls: list[str]) -> list[str]:
        """Load tools from multiple MCP servers.

        Convenience wrapper around :meth:`register_mcp_server` for batch
        loading.  Failures of individual servers do not abort the batch.

        Args:
            urls: List of MCP server URLs.

        Returns:
            List of all tool names successfully registered.
        """
        all_loaded: list[str] = []
        for url in urls:
            loaded = await self.register_mcp_server(url)
            all_loaded.extend(loaded)
        return all_loaded

    def _requirements_met(self, requires: frozenset[str]) -> bool:
        """Check whether all required feature flags are currently truthy."""
        for flag in requires:
            if not getattr(settings, flag, False):
                return False
        return True


# ---------------------------------------------------------------------------
# Default registry — built once at import, shared by all agents/graphs.
# ---------------------------------------------------------------------------


def _build_default_registry() -> ToolRegistry:
    """Build the default registry with all built-in tools.

    The agent-to-tool mapping mirrors the *current* hard-coded bindings in
    ``supervisor.py`` and ``graph.py`` so that behaviour is preserved
    exactly.  Adding a new tool to an agent is now a one-line change here
    instead of editing string-key lists inside node functions.
    """
    from app.core.langgraph.tools.ask_human import ask_human
    from app.core.langgraph.tools.asset_tools import asset_tools_list
    from app.core.langgraph.tools.code_index_tools import code_index_tools_list
    from app.core.langgraph.tools.duckduckgo_search import duckduckgo_search_tool
    from app.core.langgraph.tools.fundamentals_tools import fundamentals_tools_list
    from app.core.langgraph.tools.knowledge_base_tools import knowledge_base_tools_list
    from app.core.langgraph.tools.kline_tools import kline_tools_list
    from app.core.langgraph.tools.media_tools import media_tools_list
    from app.core.langgraph.tools.news_tools import news_tools_list
    from app.core.langgraph.tools.chart_tools import chart_tools_list
    from app.core.langgraph.tools.quote_tools import quote_tools_list
    from app.core.langgraph.tools.research_debate_tools import research_debate_tools_list
    from app.core.langgraph.tools.sdlc_tools import sdlc_tools_list

    registry = ToolRegistry()

    # -- Chat agent (LangGraphAgent) -------------------------------------
    registry.register(
        duckduckgo_search_tool,
        agents={"chat"},
        requires={"ENABLE_WEB_SEARCH"},
        tags={"search", "web"},
    )
    registry.register(ask_human, agents={"chat"}, tags={"human_loop"})

    # -- Real-time market quote tool (chat agent) -----------------------
    # Distinct from KB: KB is point-in-time snapshots, this fetches live
    # data on demand. Gated by ENABLE_REALTIME_QUOTES so the whole
    # feature can be disabled if upstream becomes unavailable.
    for quote_tool in quote_tools_list:
        registry.register(
            quote_tool,
            agents={"chat"},
            requires={"ENABLE_REALTIME_QUOTES"},
            tags={"finance", "real_time"},
        )

    # -- Fundamental analysis (chat agent) --------------------------------
    for f_tool in fundamentals_tools_list:
        registry.register(
            f_tool,
            agents={"chat"},
            requires={"ENABLE_FUNDAMENTALS"},
            tags={"finance", "fundamental"},
        )
    # -- K-line / technical indicators (chat agent) -----------------------
    for k_tool in kline_tools_list:
        registry.register(
            k_tool,
            agents={"chat"},
            requires={"ENABLE_KLINE"},
            tags={"finance", "technical"},
        )
    # -- News & sentiment (chat agent) -------------------------------------
    for n_tool in news_tools_list:
        registry.register(
            n_tool,
            agents={"chat"},
            requires={"ENABLE_NEWS"},
            tags={"finance", "news", "sentiment"},
        )

    # -- Chart generation (chat agent) -------------------------------------
    for c_tool in chart_tools_list:
        registry.register(
            c_tool,
            agents={"chat"},
            requires={"ENABLE_CHART_GEN"},
            tags={"finance", "visualization"},
        )

    # -- Research debate (chat agent, P2) ----------------------------------
    for r_tool in research_debate_tools_list:
        registry.register(
            r_tool,
            agents={"chat"},
            requires={"ENABLE_RESEARCH_DEBATE"},
            tags={"finance", "research", "debate"},
        )

    # -- Knowledge base tools (chat agent searches user-uploaded docs) --
    # Gated by ENABLE_KNOWLEDGE_BASE — invisible to LLM when flag is off.
    for kb_tool in knowledge_base_tools_list:
        registry.register(
            kb_tool,
            agents={"chat"},
            requires={"ENABLE_KNOWLEDGE_BASE"},
            tags={"knowledge", "rag"},
        )

    # -- SDLC tools ------------------------------------------------------
    # Agent-to-tool mapping mirrors the former hard-coded bindings in
    # supervisor.py _coder_node and _tester_node.
    coder_only = {
        "read_file",
        "write_file",
        "edit_file",
        "search_code_index",
        "update_code_index_file",
        "apply_multi_file_edits",
    }
    coder_and_tester = {"list_directory", "search_code"}

    for tool in sdlc_tools_list:
        agents: set[str] = set()
        requires: set[str] = set()
        tags: set[str] = set()

        if tool.name in coder_only:
            agents.add("coder")
            tags.add("file_io")
        if tool.name in coder_and_tester:
            agents.update({"coder", "tester"})
            tags.add("file_io")
        if tool.name == "analyze_image":
            agents.add("coder")
            requires.add("ENABLE_MULTIMODAL")
            tags.add("vision")
        if tool.name == "generate_and_run_script":
            agents.add("coder")
            requires.add("ENABLE_SCRIPT_EXECUTION")
            tags.add("exec")
        if tool.name == "save_output_file":
            agents.add("coder")
            requires.add("ENABLE_SCRIPT_EXECUTION")
            tags.add("io")
        if tool.name == "run_tests":
            agents.add("tester")
            tags.add("exec")
        if tool.name == "validate_changes":
            # Programmatic-only: invoked directly in _coder_node, NOT
            # LLM-bound.  Registered so get_by_name works; no agent tag.
            tags.add("validation")
        if tool.name == "security_scan":
            # Available but not currently LLM-bound; programmatic access.
            tags.add("security")

        registry.register(tool, agents=agents, tags=tags, requires=requires)

    # -- Code index / knowledge graph tools ------------------------------
    # Only search_code_index and update_code_index_file are LLM-bound (coder).
    # The rest are registered for programmatic access (smart_search is used
    # by _build_code_index_context; others are available for future use).
    coder_index_tools = {"search_code_index", "update_code_index_file"}
    for tool in code_index_tools_list:
        agents: set[str] = set()
        tags: set[str] = {"code_index"}
        if tool.name in coder_index_tools:
            agents.add("coder")
        registry.register(tool, agents=agents, tags=tags)

    # -- Media generation tools (coder can generate images/videos) -------
    # Thin wrappers around MediaGenerationService — let the coder create
    # visual assets autonomously when a task requires them.
    for tool in media_tools_list:
        registry.register(tool, agents={"coder"}, tags={"media"})

    # -- Asset management tools (coder persists URLs + declares previews) --
    # download_asset: persist temporary remote URLs (e.g. generate_image
    #   output expiring in ~24h) into the workspace sandbox.
    # register_artifact: declare which workspace files the UI should surface
    #   as previewable artifacts (HTML games, dashboards, landing pages...).
    # MUST set agents={"coder"} — without it the tool becomes programmatic-
    # only (get_by_name works, but the LLM cannot call it). Gated by the
    # ENABLE_WORKSPACE_PREVIEW flag so the whole feature can be toggled off.
    for tool in asset_tools_list:
        registry.register(
            tool,
            agents={"coder"},
            requires={"ENABLE_WORKSPACE_PREVIEW"},
            tags={"asset"},
        )

    logger.info("tool_registry_built", tool_count=len(registry.all_names()))
    return registry


# Module-level singleton — built once on first import.
default_registry: ToolRegistry = _build_default_registry()
