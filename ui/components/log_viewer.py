"""Structured log viewer with view modes and stage timeline.

Stores log entries as structured dicts in session_state and renders
them filtered by the current view mode (Verbose/Normal/Summary).

Entry schema:
    {timestamp, node, level, message}

Levels:
    debug   — streaming content chunks (Verbose only)
    info    — node start, plan/code summaries
    success — node completion
    tool    — tool call start/end
    warning — non-fatal warnings
    error   — errors

View mode level filtering:
    Verbose  — all levels
    Normal   — info, success, tool, warning, error (skips debug)
    Summary  — success, warning, error (skips debug, info, tool)
"""

import time
from typing import Dict, List

import streamlit as st

# View modes.
VIEW_MODES = ["Verbose", "Normal", "Summary"]
DEFAULT_VIEW_MODE = "Normal"

# Level icons.
LEVEL_ICON = {
    "debug": "\U0001f50d",  # 🔍
    "info": "\u2139\ufe0f",  # ℹ️
    "success": "\u2705",  # ✅
    "tool": "\U0001f527",  # 🔧
    "warning": "\u26a0\ufe0f",  # ⚠️
    "error": "\u274c",  # ❌
}

# Node display names.
NODE_LABEL = {
    "planner": "Planner",
    "coder": "Coder",
    "tester": "Tester",
    "reviewer": "Reviewer",
    "supervisor": "Supervisor",
    "system": "System",
}

# Stage order for the timeline.
STAGE_ORDER = ["planner", "coder", "tester", "reviewer"]

# Levels visible per view mode.
VIEW_MODE_LEVELS = {
    "Verbose": {"debug", "info", "success", "tool", "warning", "error"},
    "Normal": {"info", "success", "tool", "warning", "error"},
    "Summary": {"success", "warning", "error"},
}

# Timeline status icons.
STATUS_ICON = {
    "pending": "\u23f8",  # ⏸
    "running": "\U0001f504",  # 🔄
    "completed": "\u2705",  # ✅
}


def append_log_entry(node: str, message: str, level: str = "info") -> None:
    """Append a structured log entry to session_state.

    Args:
        node: Agent/node name (planner, coder, tester, reviewer, system).
        message: Log message text.
        level: One of debug/info/success/tool/warning/error.
    """
    entries: List[Dict] = st.session_state.setdefault("log_entries", [])
    entries.append(
        {
            "timestamp": time.strftime("%H:%M:%S"),
            "node": node,
            "level": level,
            "message": message,
        }
    )


def render_view_mode_selector() -> str:
    """Render the view mode radio selector.

    Returns the selected view mode string. Uses a session_state key
    so the selection persists across reruns.
    """
    return st.radio(
        "View Mode",
        VIEW_MODES,
        horizontal=True,
        key="log_view_mode",
        help=("Control log detail: Verbose = all entries, Normal = key events + tool calls, Summary = results only"),
    )


def render_log_entries(entries: List[Dict], view_mode: str) -> None:
    """Render log entries filtered by view mode.

    Builds a single markdown string for efficiency (one st.markdown
    call instead of one per entry).

    Args:
        entries: List of log entry dicts.
        view_mode: "Verbose", "Normal", or "Summary".
    """
    allowed = VIEW_MODE_LEVELS.get(view_mode, VIEW_MODE_LEVELS[DEFAULT_VIEW_MODE])
    filtered = [e for e in entries if e.get("level", "info") in allowed]

    if not filtered:
        st.caption("No log entries match the current view mode.")
        return

    lines = [_format_entry(e) for e in filtered]
    st.markdown("\n\n".join(lines))


def _format_entry(entry: Dict) -> str:
    """Format a single log entry as markdown: `[timestamp]` `[node]` icon message."""
    ts = entry.get("timestamp", "")
    node = entry.get("node", "")
    level = entry.get("level", "info")
    message = entry.get("message", "")

    icon = LEVEL_ICON.get(level, "\u2139\ufe0f")
    label = NODE_LABEL.get(node, node.capitalize() if node else "system")

    return f"`[{ts}]` `[{label}]` {icon} {message}"


def start_node_timing(node: str) -> None:
    """Mark a node as started (running) and record its start time."""
    timings: Dict = st.session_state.setdefault("node_timings", {})
    timing = timings.get(node, {})
    # Only start if not already running or completed.
    if timing.get("status") != "running":
        timings[node] = {
            "start_ts": time.time(),
            "end_ts": None,
            "duration_s": None,
            "status": "running",
        }


def end_node_timing(node: str) -> None:
    """Mark a node as completed and compute its duration."""
    timings: Dict = st.session_state.setdefault("node_timings", {})
    timing = timings.get(node, {})
    if timing.get("status") == "running":
        start_ts = timing.get("start_ts", time.time())
        end_ts = time.time()
        timing["end_ts"] = end_ts
        timing["duration_s"] = end_ts - start_ts
        timing["status"] = "completed"


def render_stage_timeline(timings: Dict) -> None:
    """Render the SDLC stage timeline as a horizontal flow.

    Shows each stage with status icon and duration (if completed).
    Stages are rendered in SDLC order: planner → coder → tester → reviewer.

    Args:
        timings: Dict mapping node name to timing dict.
    """
    if not timings:
        return

    parts = []
    for stage in STAGE_ORDER:
        timing = timings.get(stage)
        if not timing:
            status = "pending"
            duration = None
        else:
            status = timing.get("status", "pending")
            duration = timing.get("duration_s")

        icon = STATUS_ICON.get(status, "\u23f8")
        label = NODE_LABEL.get(stage, stage)

        if duration is not None:
            parts.append(f"{icon} **{label}** ({duration:.1f}s)")
        elif status == "running":
            parts.append(f"{icon} **{label}** (running...)")
        else:
            parts.append(f"{icon} {label}")

    st.markdown(" \u2500\u2500 ".join(parts))


def render_log_area(view_mode: str) -> None:
    """Render the persisted log entries (post-completion review).

    Called when the task is not running but log entries exist.
    Shows the view-mode-filtered log for review.

    Args:
        view_mode: Current view mode string.
    """
    entries = st.session_state.get("log_entries", [])
    if not entries:
        return

    st.subheader("Execution Log")

    timings = st.session_state.get("node_timings", {})
    render_stage_timeline(timings)

    st.markdown("---")
    render_log_entries(entries, view_mode)
