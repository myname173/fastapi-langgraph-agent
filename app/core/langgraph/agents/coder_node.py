"""Coder node — writes and edits code using file tools.

This is the largest and most complex SDLC node.  To keep it maintainable,
the logic is split into three responsibility layers:

1. **Tool selection** — ``ctx.registry.get_for_agent("coder")`` replaces the
   former 14 hard-coded ``self.sdlc_tools_by_name["..."]`` lookups.
2. **LLM execution** — bind tools, invoke, dispatch tool-calls directly.
3. **Post-processing** — smart-edit application, post-edit validation,
   retry counting, and human-approval escalation.

Extracted from the former ``SupervisorGraph._coder_node`` method (238 lines).
"""

import re
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import Command

from app.core.config import settings
from app.core.langgraph.agents.context import AgentContext
from app.core.langgraph.agents.supervisor_node import NodeCallable
from app.core.logging import logger
from app.core.metrics import (
    mcp_tool_calls_total,
    sdlc_agent_errors_total,
    sdlc_agent_execution_seconds,
)
from app.core.prompts import load_agent_prompt
from app.schemas import CoderOutput, SDLCState
from app.services.llm.usage_tracker import set_llm_context
from app.utils import prepare_sdlc_messages

# Tools that actually mutate the workspace. Only these may contribute a
# ``code_changes`` entry for the UI/state; read-only tools (read_file,
# list_directory, search_code, git_diff, …) must not, otherwise a pure
# inspection pass is reported as a code change with empty content.
_FILE_MUTATING_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "apply_multi_file_edits",
        "save_output_file",
        "generate_and_run_script",
    }
)


def _normalise_change_type(edit_type: object) -> str:
    """Map a ``CodeEdit.edit_type`` onto a valid ``FileChange.change_type``.

    The two schemas use deliberately different vocabularies: an *edit* is
    ``replace`` / ``insert`` / ``create`` / ``delete``, whereas a *file change*
    reported to the API/UI is only ``create`` / ``modify`` / ``delete``.
    Copying one into the other verbatim produced ``change_type="replace"``,
    which fails ``SDLCResponse`` validation at serialisation time and turns an
    otherwise successful workflow into an HTTP 500.

    Anything that is not an explicit create/delete is a modification, which is
    the correct user-facing description for replace/insert (and for any
    unexpected value, so a novel edit type can never break the response).
    """
    value = str(edit_type or "").strip().lower()
    if value == "create":
        return "create"
    if value == "delete":
        return "delete"
    return "modify"


# ---------------------------------------------------------------------------
# Helper: build reflection context for retry attempts (Reflexion pattern).
# ---------------------------------------------------------------------------


def _build_reflection_context(
    coder_retry_count: int,
    validation_results: list[dict],
    feedback: list[dict],
) -> str:
    """Build a structured reflection context for retry attempts.

    Implements the Reflexion pattern: when the coder is retrying a failed
    attempt, inject the previous failure reasons and improvement suggestions
    into the system prompt so the LLM can learn from the mistake.

    Args:
        coder_retry_count: Current retry count from state (0 = first attempt).
        validation_results: List of validation result dicts.
        feedback: List of feedback dicts from reviewer/supervisor.

    Returns:
        Structured reflection text, or a no-reflection placeholder when
        this is the first attempt (retry_count == 0).
    """
    if coder_retry_count == 0:
        return "(First attempt — no prior failures to reflect on.)"

    sections: list[str] = []

    # Section 1: Previous validation failures
    if validation_results:
        last_validation = validation_results[-1]
        validation_lines: list[str] = []
        if not last_validation.get("passed", True):
            msg = last_validation.get("message", "Unknown error")
            validation_lines.append(f"- Validation error: {msg}")
            details = last_validation.get("details", {})
            if isinstance(details, dict):
                for check, result in details.items():
                    if isinstance(result, dict) and not result.get("passed", True):
                        validation_lines.append(f"  - {check}: {result.get('message', 'failed')}")
                    elif isinstance(result, str):
                        validation_lines.append(f"  - {check}: {result}")
            if validation_lines:
                sections.append("### Previous attempt failure reasons\n" + "\n".join(validation_lines))

    # Section 2: Reviewer/system feedback as improvement suggestions
    if feedback:
        feedback_lines: list[str] = []
        for fb in feedback:
            if isinstance(fb, dict):
                agent = fb.get("agent_name", "unknown")
                content = fb.get("content", "")
                severity = fb.get("severity", "")
                prefix = f"[{severity}] " if severity else ""
                feedback_lines.append(f"- ({agent}) {prefix}{content}")
        if feedback_lines:
            sections.append("### Improvement suggestions\n" + "\n".join(feedback_lines))

    if not sections:
        return f"(Retry attempt {coder_retry_count}/{settings.MAX_CODER_RETRIES} — no specific feedback available.)"

    header = (
        f"**Retry attempt {coder_retry_count}/{settings.MAX_CODER_RETRIES}. "
        "Learn from the previous failure below and do NOT repeat the same mistakes.**"
    )
    return header + "\n\n" + "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Helper: build semantic code-index context for the coder prompt.
# ---------------------------------------------------------------------------


async def _build_code_index_context(ctx: AgentContext, thread_id: str, requirements: str, user_id: str = "") -> str:
    """Build a context string from semantic code index and knowledge base.

    Searches the workspace code index (per-thread) and the user's knowledge
    base (per-user) in parallel, merging results with source labels. KB
    search is opt-in via ``ENABLE_KNOWLEDGE_BASE`` Feature Flag.

    Extracted from the former ``SupervisorGraph._build_code_index_context``.
    """
    if not settings.CODE_INDEX_ENABLED:
        return "Code index is disabled."

    if not requirements or len(requirements.strip()) < 10:
        return "No requirements provided; skipping code index lookup."

    sections: list[str] = []

    # --- Code Index Search (per-thread, workspace-scoped) ---
    try:
        if settings.ENABLE_SMART_CODER and settings.ENABLE_SMART_SEARCH:
            from app.core.langgraph.tools.sdlc_tools import get_workspace_path

            workspace = str(get_workspace_path(thread_id, "."))
            results = await ctx.smart_search_service.smart_search(thread_id, requirements, workspace, limit=8)
        else:
            results = await ctx.code_index_service.search(thread_id, requirements, limit=8)

        if results:
            lines = ["Relevant code from semantic search:"]
            for i, result in enumerate(results, 1):
                lines.append(f"\n--- Result {i} (score: {result.get('score', 0):.3f}) ---")
                lines.append(f"File: {result.get('file', 'unknown')}")
                if result.get("summary"):
                    lines.append(f"Summary: {result.get('summary')}")
                lines.append(f"Content:\n{result.get('content', '')[:500]}")
            sections.append("\n".join(lines))

            logger.debug(
                "code_index_context_built",
                thread_id=thread_id,
                result_count=len(results),
            )
    except Exception as e:
        logger.error("code_index_context_failed", thread_id=thread_id, error=str(e))
        sections.append(f"Code index lookup failed: {str(e)}")

    # --- Knowledge Base Search (per-user, shared across tasks) ---
    if settings.ENABLE_KNOWLEDGE_BASE and user_id:
        try:
            kb_results = await ctx.knowledge_base_service.search(user_id=str(user_id), query=requirements)
            if kb_results:
                lines = ["Relevant documents from knowledge base:"]
                for i, result in enumerate(kb_results, 1):
                    lines.append(f"\n--- KB Result {i} (score: {result.get('score', 0):.3f}) ---")
                    lines.append(f"Source: {result.get('filename', 'unknown')}")
                    lines.append(f"Content:\n{result.get('content', '')[:500]}")
                sections.append("\n".join(lines))

                logger.debug(
                    "kb_context_built",
                    user_id=user_id,
                    result_count=len(kb_results),
                )
        except Exception as e:
            logger.error("kb_context_failed", user_id=user_id, error=str(e))
            # Non-blocking — code index results are still useful

    if not sections:
        return "No relevant code or documents found."

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Helper: apply smart edits + post-edit validation (post-processing layer).
# ---------------------------------------------------------------------------


async def _apply_smart_edits_and_validate(
    ctx: AgentContext,
    thread_id: str,
    parsed: CoderOutput,
    config: RunnableConfig,
    current_retry_count: int,
) -> tuple[list[dict], list[dict], bool]:
    """Apply smart edits, run validation, and determine approval escalation.

    Args:
        ctx: Agent context with registry and services.
        thread_id: Workspace thread ID.
        parsed: Parsed CoderOutput from the LLM response.
        config: LangGraph runnable config.
        current_retry_count: Current coder retry count from state.

    Returns:
        Tuple of (code_changes_data, validation_results, needs_approval).
    """
    code_changes_data: list[dict] = []
    validation_results: list[dict] = []
    needs_approval = parsed.needs_human_approval

    if not needs_approval and parsed.edits:
        unique_files = len(set(e.file_path for e in parsed.edits))
        if unique_files > settings.SMART_EDIT_AUTO_APPROVE_THRESHOLD:
            needs_approval = True
            parsed.approval_reason = (
                f"Auto-flagged: {unique_files} files modified exceeds "
                f"threshold of {settings.SMART_EDIT_AUTO_APPROVE_THRESHOLD}"
            )

    if parsed.edits and not needs_approval and settings.ENABLE_SMART_EDITING:
        edit_dicts = [e.model_dump() for e in parsed.edits]
        try:
            multi_tool = ctx.registry.get_by_name("apply_multi_file_edits")
            result = await multi_tool.ainvoke(
                {"thread_id": thread_id, "edits": edit_dicts},
                config=config,
            )
            if isinstance(result, dict) and result.get("success"):
                code_changes_data.extend(
                    [
                        {
                            "file_path": r.get("file", ""),
                            "change_type": "modify" if r.get("status") == "modified" else "create",
                            "content": "",
                            "commit_message": r.get("reason", ""),
                        }
                        for r in result.get("results", [])
                    ]
                )
                logger.info(
                    "sdlc_coder_smart_edits_applied",
                    edit_count=result.get("applied_count", 0),
                    files_affected=result.get("files_affected", 0),
                )

                validation_results = []
                if settings.ENABLE_POST_EDIT_VALIDATION:
                    modified_files = [r.get("file", "") for r in result.get("results", []) if r.get("file")]
                    try:
                        validate_tool = ctx.registry.get_by_name("validate_changes")
                        validation_result = await validate_tool.ainvoke(
                            {"thread_id": thread_id, "file_paths": modified_files},
                            config=config,
                        )
                        validation_results = [validation_result]
                        logger.info(
                            "sdlc_coder_validation_complete",
                            passed=validation_result.get("passed", False),
                            lint_errors=len(validation_result.get("lint_errors", [])),
                            security_issues=len(validation_result.get("security_issues", [])),
                        )
                    except Exception as val_err:
                        logger.error("sdlc_coder_validation_failed", error=str(val_err))
                        validation_results = [
                            {
                                "passed": False,
                                "message": f"Validation failed: {val_err}",
                                "lint_errors": [],
                                "security_issues": [],
                                "warnings": [{"type": "validation_error", "message": str(val_err)}],
                            }
                        ]

                if validation_results and not validation_results[0].get("passed"):
                    next_retry = current_retry_count + 1
                    if next_retry >= settings.MAX_CODER_RETRIES:
                        needs_approval = True
                        parsed.approval_reason = (
                            f"Validation failed after {next_retry} attempts. "
                            f"Last error: {validation_results[0].get('message', '')}"
                        )
                    else:
                        logger.info(
                            "sdlc_coder_validation_failed_retry",
                            retry_count=next_retry,
                            max_retries=settings.MAX_CODER_RETRIES,
                        )

        except Exception as edit_err:
            logger.exception("sdlc_coder_smart_edits_failed", error=str(edit_err))
            parsed.approval_reason = f"Smart edit failed: {edit_err}"
            needs_approval = True
            # Fallback: surface the intended edits so the UI still shows
            # what the coder wanted to write. Without this the results
            # panel would be empty even though the LLM produced edits.
            code_changes_data = [
                {
                    "file_path": e.file_path,
                    "change_type": _normalise_change_type(e.edit_type),
                    "content": e.new_str,
                    "commit_message": f"fallback (smart edit failed): {e.edit_type}",
                }
                for e in parsed.edits
            ]

    # Process parsed.code_changes (FileChange list) — write new files via
    # write_file tool. Previously only parsed.edits were handled, so tasks
    # that output code_changes (e.g. "create a new HTML page") resulted in
    # an empty workspace and an infinite coder loop (supervisor guard sees
    # plan_present_code_empty → re-routes to coder forever).
    if parsed.code_changes:
        if not needs_approval:
            try:
                write_tool = ctx.registry.get_by_name("write_file")
                for fc in parsed.code_changes:
                    if fc.change_type == "delete":
                        logger.warning(
                            "sdlc_coder_skip_delete",
                            file_path=fc.file_path,
                            reason="delete_file tool not available",
                        )
                        continue
                    try:
                        await write_tool.ainvoke(
                            {
                                "thread_id": thread_id,
                                "path": fc.file_path,
                                "content": fc.content,
                            },
                            config=config,
                        )
                        code_changes_data.append(
                            {
                                "file_path": fc.file_path,
                                "change_type": fc.change_type,
                                "content": fc.content,
                                "commit_message": fc.commit_message or f"coder: {fc.change_type}",
                            }
                        )
                    except Exception as write_err:
                        logger.error(
                            "sdlc_coder_write_file_failed",
                            file_path=fc.file_path,
                            error=str(write_err),
                        )
                        code_changes_data.append(
                            {
                                "file_path": fc.file_path,
                                "change_type": fc.change_type,
                                "content": fc.content,
                                "commit_message": f"fallback (write failed): {write_err}",
                            }
                        )
            except Exception as write_resolve_err:
                # write_file tool not registered — surface changes without writing
                logger.error(
                    "sdlc_coder_write_tool_unavailable",
                    error=str(write_resolve_err),
                )
                code_changes_data.extend(
                    [
                        {
                            "file_path": fc.file_path,
                            "change_type": fc.change_type,
                            "content": fc.content,
                            "commit_message": fc.commit_message or f"fallback (no write tool): {fc.change_type}",
                        }
                        for fc in parsed.code_changes
                    ]
                )
        else:
            # Approval needed — surface intended changes without writing
            code_changes_data.extend(
                [
                    {
                        "file_path": fc.file_path,
                        "change_type": fc.change_type,
                        "content": fc.content,
                        "commit_message": fc.commit_message or f"pending approval: {fc.change_type}",
                    }
                    for fc in parsed.code_changes
                ]
            )

    return code_changes_data, validation_results, needs_approval


# ---------------------------------------------------------------------------
# Helper: extract code_changes from a raw (non-JSON) LLM response.
# ---------------------------------------------------------------------------

# Markers to detect plain-code responses and infer a sensible file name.
# Checked against the first ~256 chars of the LLM output.
_CODE_MARKERS: list[tuple[str, str, str]] = [
    ("<!doctype", "html", "index.html"),
    ("<html", "html", "index.html"),
    ("#!/usr/bin/env python", "python", "main.py"),
    ("#!/bin/bash", "bash", "script.sh"),
    ("def main(", "python", "main.py"),
    ("import asyncio", "python", "main.py"),
    ("from fastapi", "python", "main.py"),
    ("function(", "javascript", "index.js"),
    ("const express", "javascript", "server.js"),
    ("import React", "javascript", "App.jsx"),
    ("package main", "go", "main.go"),
    ("public class", "java", "Main.java"),
    ("#include", "cpp", "main.cpp"),
]


def _extract_code_fallback(content: str) -> list[dict]:
    """Extract code_changes from a raw (non-JSON) LLM response.

    Bailian qwen-plus frequently ignores the ``CoderOutput`` JSON contract
    and returns plain code (e.g. a full ``<!DOCTYPE html>`` document).
    Without this fallback, JSON parsing fails → ``code_changes=[]`` →
    supervisor guard forces back to coder → LLM returns plain code again
    → infinite loop.

    Strategy:
      1. Try to find a ```json ... ``` fenced block and parse it.
      2. If that fails, detect plain code by markers and wrap the whole
         content as a single ``create`` change with an inferred file name.
      3. If nothing matches but content is substantial, treat it as a
         generic ``output.txt`` so the UI still shows something.
    """
    if not content or not content.strip():
        return []

    text = content.strip()

    # Step 1: try to rescue a JSON code block the LLM may have wrapped.
    json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if json_match:
        try:
            parsed = CoderOutput.model_validate_json(json_match.group(1))
            if parsed.edits:
                return [
                    {
                        "file_path": e.file_path,
                        "change_type": _normalise_change_type(e.edit_type),
                        "content": e.new_str,
                        "commit_message": f"fallback (extracted from code block): {e.edit_type}",
                    }
                    for e in parsed.edits
                ]
            if parsed.code_changes:
                return [fc.model_dump() for fc in parsed.code_changes]
        except Exception:
            pass  # fall through to plain-code detection

    # Step 2: detect plain code by markers in the first 256 chars.
    head = text[:256].lower()
    for marker, lang, default_name in _CODE_MARKERS:
        if marker in head:
            logger.warning(
                "sdlc_coder_raw_code_fallback",
                detected_lang=lang,
                content_length=len(text),
            )
            return [
                {
                    "file_path": default_name,
                    "change_type": "create",
                    "content": text,
                    "commit_message": f"fallback: LLM returned raw {lang} instead of JSON",
                }
            ]

    # Step 3: last resort — if content is substantial, treat as code.
    if len(text) > 100:
        logger.warning(
            "sdlc_coder_raw_code_fallback",
            detected_lang="unknown",
            content_length=len(text),
        )
        return [
            {
                "file_path": "output.txt",
                "change_type": "create",
                "content": text,
                "commit_message": "fallback: raw LLM output (unrecognised format)",
            }
        ]

    return []


# ---------------------------------------------------------------------------
# Main factory.
# ---------------------------------------------------------------------------


def make_coder_node(ctx: AgentContext) -> NodeCallable:
    """Create the coder node function.

    The node binds the coder tool-set from the registry, invokes the LLM,
    dispatches tool-calls, and post-processes structured output for smart
    edits, validation, and approval escalation.
    """

    async def coder_node(state: SDLCState, config: RunnableConfig) -> Command:
        # Layer 1: Tool selection — registry replaces 14 string-key lookups.
        file_tools = ctx.registry.get_for_agent("coder")

        # Per-task coder model override (SDLC coder model selection feature).
        # state["coder_model"] is set by the API layer from SDLCRequest.coder_model
        # and persisted in the checkpoint, so reject->rerun keeps the same model.
        # None => fall back to the router default (kimi-k2.7-code).
        model_name = state.get("coder_model") or ctx.model_router.get_model_for("coder")

        try:
            thread_id = config.get("configurable", {}).get("thread_id", "default")
            code_index_context = await _build_code_index_context(
                ctx, thread_id, state.get("requirements", ""), state.get("user_id", "")
            )

            system_prompt = load_agent_prompt(
                "coder",
                requirements=state.get("requirements", ""),
                plan=state.get("plan", []),
                code_changes=state.get("code_changes", []),
                feedback=state.get("feedback", []),
                code_index_context=code_index_context,
                dependency_impact="",
                image_contexts=state.get("image_contexts", []),
                reflection_context=_build_reflection_context(
                    state.get("coder_retry_count", 0),
                    state.get("validation_results", []),
                    state.get("feedback", []),
                ),
            )
            messages = prepare_sdlc_messages(state["messages"], system_prompt)

            # Layer 2: LLM execution + tool-call dispatch.
            with (
                sdlc_agent_execution_seconds.labels(agent_name="coder", step="coding", status="success").time(),
                set_llm_context("coder", "coding"),
            ):
                response = await ctx.llm_service.call(
                    messages,
                    model_name=model_name,
                    tools=file_tools,
                )

            if response.tool_calls:
                # Execute tool calls directly instead of via ToolNode.
                # ToolNode in newer LangGraph versions requires a runtime
                # config key ("__pregel_runtime") that is only injected when
                # called from within a graph node — direct ainvoke() does
                # not have it, causing "Missing required config key 'N/A'
                # for 'tools'" errors.
                tools_by_name = {t.name: t for t in file_tools}
                tool_messages: list[ToolMessage] = []
                code_changes = []
                for tool_call in response.tool_calls:
                    tool_name = tool_call["name"]
                    args = tool_call.get("args", {})

                    # Execute tool
                    if tool_name in tools_by_name:
                        try:
                            result = await tools_by_name[tool_name].ainvoke(args, config=config)
                            tool_messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))
                            # MCP tool invocation metric — success path.
                            if tool_name.startswith("mcp_"):
                                mcp_tool_calls_total.labels(tool=tool_name, status="success").inc()
                        except Exception as tool_err:
                            tool_messages.append(
                                ToolMessage(
                                    content=f"Tool {tool_name} failed: {tool_err}",
                                    tool_call_id=tool_call["id"],
                                )
                            )
                            # MCP tool invocation metric — failure path.
                            if tool_name.startswith("mcp_"):
                                mcp_tool_calls_total.labels(tool=tool_name, status="failed").inc()
                    else:
                        # C-2 fix: defensive fallback for tools missing from the
                        # agent-scoped subset but present in the global registry
                        # (e.g. apply_multi_file_edits). Resolve programmatically
                        # so a model-requested tool is not silently rejected.
                        fallback_tool = None
                        try:
                            if hasattr(ctx, "registry") and ctx.registry.has(tool_name):
                                fallback_tool = ctx.registry.get_by_name(tool_name)
                        except Exception:
                            fallback_tool = None
                        if fallback_tool is not None:
                            try:
                                result = await fallback_tool.ainvoke(args, config=config)
                                tool_messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))
                                continue
                            except Exception as fb_err:
                                tool_messages.append(
                                    ToolMessage(
                                        content=f"Tool {tool_name} failed: {fb_err}",
                                        tool_call_id=tool_call["id"],
                                    )
                                )
                                continue
                        tool_messages.append(
                            ToolMessage(
                                content=f"Unknown tool: {tool_name}",
                                tool_call_id=tool_call["id"],
                            )
                        )
                        # Defensive: unknown mcp_* tool means registry desync —
                        # surface it as a failure so dashboards can catch it.
                        if tool_name.startswith("mcp_"):
                            mcp_tool_calls_total.labels(tool=tool_name, status="failed").inc()

                    # Construct code_changes for UI display (from tool_call args).
                    # Read-only tools contribute nothing — recording them would
                    # surface empty "changes" for files the coder merely read.
                    if tool_name == "apply_multi_file_edits":
                        edits = args.get("edits", [])
                        for edit in edits:
                            code_changes.append(
                                {
                                    "file_path": edit.get("file_path", ""),
                                    "change_type": _normalise_change_type(edit.get("edit_type")),
                                    "content": edit.get("new_str", ""),
                                    "commit_message": f"smart edit: {edit.get('edit_type', 'replace')}",
                                }
                            )
                    elif tool_name in _FILE_MUTATING_TOOLS:
                        change_type = "create" if "create" in tool_name or "write" in tool_name else "modify"
                        code_changes.append(
                            {
                                "file_path": args.get("path", "") or args.get("file_path", ""),
                                "change_type": change_type,
                                "content": args.get("content", "") or args.get("new_str", ""),
                                "commit_message": f"tool: {tool_name}",
                            }
                        )

                logger.info(
                    "sdlc_coder_tool_calls",
                    tool_call_count=len(response.tool_calls),
                    code_change_count=len(code_changes),
                )

                update: dict[str, Any] = {
                    "messages": [response] + tool_messages,
                    "code_changes": code_changes,
                    "status": "coding",
                }
                if not code_changes:
                    # The turn only inspected the workspace (read_file /
                    # list_directory / search_code …) and wrote nothing. Without
                    # a retry budget the supervisor guard (plan present, code
                    # empty) bounces straight back here forever, spinning until
                    # recursion_limit. Mirror the no-code guard used by the
                    # non-tool-call path so the circuit breaker can trip instead.
                    current_retry = state.get("coder_retry_count", 0) + 1
                    update["coder_retry_count"] = current_retry
                    update["errors"] = state.get("errors", []) + [
                        {
                            "agent_name": "coder",
                            "content": (
                                f"Coder inspected the workspace but produced no file "
                                f"changes (retry {current_retry}/{settings.MAX_CODER_RETRIES})"
                            ),
                            "severity": "medium",
                        }
                    ]

                return Command(update=update, goto="supervisor")

            # Layer 3: Post-processing — parse output, apply smart edits,
            # validate, count retries, escalate to human approval.
            parse_failure_retry = 0  # set in except block when parse fails
            try:
                # C-1 fix: DashScope-compatible mode may wrap the JSON in a
                # fenced code block or prepend reasoning text. Extract the bare
                # JSON blob before validation instead of parsing raw content.
                from langchain_core.exceptions import OutputParserException as _OPE

                from app.core.langgraph.agents.context import (
                    _recover_from_parse_error,
                )

                _raw = response.content if isinstance(response.content, str) else ""
                try:
                    parsed = CoderOutput.model_validate_json(_raw)
                except Exception as _first_err:
                    # Compatible-mode providers occasionally return a bare JSON
                    # array instead of the {plan,edits,code_changes} wrapper. Try
                    # to recover by wrapping the array into the first matching
                    # List-typed field (e.g. plan / edits / code_changes).
                    _recovered = _recover_from_parse_error(
                        _OPE(f"coder parse failed: {_first_err}", llm_output=_raw),
                        response_format=CoderOutput,
                        agent_name="coder",
                        step="coder_output",
                    )
                    if _recovered is not None:
                        parsed = _recovered
                    else:
                        raise _first_err
                code_changes_data, validation_results, needs_approval = await _apply_smart_edits_and_validate(
                    ctx, thread_id, parsed, config, state.get("coder_retry_count", 0)
                )
                # A recovered CoderOutput may contain only a `plan` (the model
                # emitted planning steps instead of actual code edits). Treat a
                # parsed result with no real file/code edits as a failed parse so
                # the supervisor circuit breaker can retry rather than shipping
                # empty/invalid "code" into the tests.
                if not code_changes_data and (
                    not getattr(parsed, "edits", None) and not getattr(parsed, "code_changes", None)
                ):
                    raise ValueError("CoderOutput has plan but no edits/code_changes")
            except Exception as parse_err:
                # Parsing or smart-edit failed. Previously this swallowed
                # everything and returned code_changes=[] — making the
                # results panel show nothing AND causing an infinite loop
                # (supervisor guard sees plan_present_code_empty → coder
                # → LLM returns non-JSON again → loop forever).
                logger.error(
                    "sdlc_coder_postprocess_failed",
                    error=str(parse_err),
                    has_tool_calls=bool(response.tool_calls),
                )
                parsed = None
                needs_approval = False
                validation_results = []
                # CRITICAL: extract code from raw LLM output so the workflow
                # can proceed. Without this, code_changes stays empty and
                # the supervisor guard forces an infinite coder loop.
                code_changes_data = _extract_code_fallback(response.content)
                # P0 fix: count parse failures as retries so the supervisor
                # circuit breaker (MAX_CODER_RETRIES) can trip and terminate
                # the loop. Previously parse failures were swallowed here,
                # errors stayed empty, and supervisor re-routed to coder
                # forever because the fallback code looked "incomplete".
                parse_failure_retry = state.get("coder_retry_count", 0) + 1

            logger.info(
                "sdlc_coder_completed",
                code_change_count=len(code_changes_data),
                needs_approval=needs_approval,
            )

            update_dict: dict[str, Any] = {
                "messages": [{"role": "assistant", "content": response.content}],
                "code_changes": code_changes_data,
                "status": "coding",
                "validation_results": validation_results,
                "coder_retry_count": state.get("coder_retry_count", 0),
            }

            # P0 fix: parse-failure path increments retry count + logs error
            # so supervisor circuit breaker can trip.
            if parsed is None:
                update_dict["coder_retry_count"] = parse_failure_retry
                update_dict["errors"] = state.get("errors", []) + [
                    {
                        "agent_name": "coder",
                        "content": f"Structured output parse failed (retry {parse_failure_retry}/{settings.MAX_CODER_RETRIES})",
                        "severity": "high",
                    }
                ]
            elif validation_results and not validation_results[0].get("passed"):
                current_retry = state.get("coder_retry_count", 0) + 1
                update_dict["coder_retry_count"] = current_retry
            elif not code_changes_data:
                # Parse succeeded but produced no code changes — treat as
                # retry to avoid infinite loop (supervisor guard sees
                # plan_present_code_empty → re-routes to coder forever).
                current_retry = state.get("coder_retry_count", 0) + 1
                update_dict["coder_retry_count"] = current_retry
                update_dict["errors"] = state.get("errors", []) + [
                    {
                        "agent_name": "coder",
                        "content": f"Coder produced no code changes (retry {current_retry}/{settings.MAX_CODER_RETRIES})",
                        "severity": "medium",
                    }
                ]

            if needs_approval:
                update_dict["human_approval_needed"] = True
                update_dict["artifacts"] = [
                    {
                        "type": "approval_request",
                        "agent": "coder",
                        "reason": parsed.approval_reason if parsed else "Unknown",
                        "edit_count": len(parsed.edits) if parsed and parsed.edits else 0,
                    }
                ]

            if parsed and parsed.impact_report:
                artifacts = update_dict.get("artifacts", [])
                artifacts.append(
                    {
                        "type": "impact_report",
                        "agent": "coder",
                        "content": parsed.impact_report,
                    }
                )
                update_dict["artifacts"] = artifacts

            return Command(
                update=update_dict,
                goto="supervisor",
            )
        except Exception as e:
            sdlc_agent_errors_total.labels(agent_name="coder", step="coding").inc()
            logger.error("sdlc_agent_parsing_failed", agent_name="coder", error=str(e))
            return Command(
                update={
                    "messages": [{"role": "assistant", "content": f"Coder error: {str(e)}"}],
                    "errors": [{"agent_name": "coder", "content": str(e), "severity": "high"}],
                    "status": "coding",
                    "coder_retry_count": state.get("coder_retry_count", 0) + 1,
                },
                goto="supervisor",
            )

    return coder_node
