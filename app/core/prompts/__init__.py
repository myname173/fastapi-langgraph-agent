"""This file contains the prompts for the agent."""

import os
from datetime import datetime
from typing import Optional

from app.core.config import settings

_PROMPTS_DIR = os.path.dirname(__file__)

# Read templates once at module load — no file I/O per request
with open(os.path.join(_PROMPTS_DIR, "system.md"), "r") as _f:
    _SYSTEM_PROMPT_TEMPLATE = _f.read()

with open(os.path.join(_PROMPTS_DIR, "session_title.md"), "r") as _f:
    SESSION_TITLE_PROMPT = _f.read()

# Optional investment-advisor system prompt (loaded lazily; only when the
# file exists so the generic template remains the default fallback).
_INVESTMENT_ADVISOR_TEMPLATE: Optional[str] = None
_invest_path = os.path.join(_PROMPTS_DIR, "investment_advisor.md")
if os.path.exists(_invest_path):
    with open(_invest_path, "r", encoding="utf-8") as _f:
        _INVESTMENT_ADVISOR_TEMPLATE = _f.read()

# Cache key: (agent_name, version) → template string
_agent_prompt_cache: dict[tuple[str, str], str] = {}


def load_system_prompt(username: Optional[str] = None, vision_context: str = "", **kwargs):
    """Load the system prompt from the cached template.

    When ``settings.ENABLE_INVESTMENT_ADVISOR`` is true and the advisor
    template exists, the chat agent is switched to the investment-advisor
    persona (three-pillar framework + mandatory disclaimer gate).

    Args:
        username: Optional display name, injected as user context.
        vision_context: Optional text block summarizing user-uploaded images.
            Injected via literal replace (not ``.format``) so curly braces in
            the user/VLM-provided text never raise.
        **kwargs: Additional template variables (e.g. ``long_term_memory``).
    """
    if settings.ENABLE_INVESTMENT_ADVISOR and _INVESTMENT_ADVISOR_TEMPLATE:
        template = _INVESTMENT_ADVISOR_TEMPLATE
    else:
        template = _SYSTEM_PROMPT_TEMPLATE

    user_context = f"# User\nYou are talking to {username}.\n" if username else ""
    system_prompt = template.format(
        agent_name=settings.PROJECT_NAME + " Agent",
        current_date_and_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_context=user_context,
        **kwargs,
    )
    return system_prompt.replace("<<VISION_CONTEXT>>", vision_context or "")


def _resolve_prompt_version(agent_name: str, version: Optional[str]) -> str:
    """Resolve the effective prompt version for an agent.

    Resolution order:
    1. Explicit version argument (if provided)
    2. settings.PROMPT_VERSIONS[agent_name] (if configured)
    3. Empty string (falls back to unversioned {agent_name}.md)
    """
    if version:
        return version
    return settings.PROMPT_VERSIONS.get(agent_name, "")


def _find_prompt_file(agent_name: str, version: str) -> str:
    """Locate the prompt file for an agent, respecting versioning.

    Lookup order:
    1. If version is non-empty: {agent_name}.{version}.md (e.g. coder.v2.md)
    2. Fallback: {agent_name}.md (unversioned, backward compatible)

    Raises:
        ValueError: If neither file exists.
    """
    if version:
        versioned_path = os.path.join(_PROMPTS_DIR, f"{agent_name}.{version}.md")
        if os.path.exists(versioned_path):
            return versioned_path

    unversioned_path = os.path.join(_PROMPTS_DIR, f"{agent_name}.md")
    if os.path.exists(unversioned_path):
        return unversioned_path

    raise ValueError(
        f"Prompt template not found for agent '{agent_name}'"
        f"{f' (version={version})' if version else ''}. "
        f"Looked for: {agent_name}.{version}.md and {agent_name}.md"
    )


def load_agent_prompt(agent_name: str, version: Optional[str] = None, **kwargs) -> str:
    """Load an agent-specific prompt template from the prompts directory.

    Supports versioned prompts via two mechanisms:
    1. Explicit version argument: load_agent_prompt("coder", version="v2")
    2. Configuration-driven: settings.PROMPT_VERSIONS = {"coder": "v2"}

    File lookup falls back from versioned ({agent}.{version}.md) to
    unversioned ({agent}.md) for backward compatibility.

    Args:
        agent_name: Name of the agent (e.g., "supervisor", "planner", "coder")
        version: Optional prompt version (e.g., "v1", "v2"). If None, falls
            back to settings.PROMPT_VERSIONS.get(agent_name, "").
        **kwargs: Additional variables to substitute in the template

    Returns:
        The formatted prompt string

    Raises:
        ValueError: If no prompt template file is found
    """
    resolved_version = _resolve_prompt_version(agent_name, version)
    cache_key = (agent_name, resolved_version)

    if cache_key not in _agent_prompt_cache:
        filepath = _find_prompt_file(agent_name, resolved_version)
        with open(filepath, "r", encoding="utf-8") as f:
            _agent_prompt_cache[cache_key] = f.read()

    return _agent_prompt_cache[cache_key].format(
        project_name=settings.PROJECT_NAME,
        current_date_and_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **kwargs,
    )


def get_prompt_version(agent_name: str, version: Optional[str] = None) -> str:
    """Return the resolved prompt version for tracing/metrics.

    Use this to attach the active prompt version to Langfuse metadata
    or Prometheus labels without loading the prompt itself.
    """
    return _resolve_prompt_version(agent_name, version) or "default"
