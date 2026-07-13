"""LangGraph tools for enhanced language model capabilities.

This package contains custom tools that can be used with LangGraph to extend
the capabilities of language models. Currently includes tools for web search
and other external integrations.
"""

from langchain_core.tools.base import BaseTool

from .ask_human import ask_human
from .code_index_tools import code_index_tools_list
from .duckduckgo_search import duckduckgo_search_tool
from .registry import ToolRegistry, default_registry
from .sdlc_tools import sdlc_tools_list

# Backward-compatible flat lists (kept for any external consumers).
tools: list[BaseTool] = [duckduckgo_search_tool, ask_human]
sdlc_tools: list[BaseTool] = sdlc_tools_list + code_index_tools_list

__all__ = [
    "ask_human",
    "code_index_tools_list",
    "duckduckgo_search_tool",
    "sdlc_tools_list",
    "sdlc_tools",
    "tools",
    "ToolRegistry",
    "default_registry",
]
