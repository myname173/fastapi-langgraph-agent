"""Enhanced sidebar — session manager with task search, filter, and status badges.

Inspired by Claude Code desktop's multi-session sidebar: tasks are
displayed as cards with status badges, searchable and filterable.
Account/settings/feature-flags are condensed to keep the focus on tasks.

Phase 4 enhancements:
- Task cards show requirements preview + relative timestamp
- Task count summary (total / active / done / error)
- Search matches requirements text (not just ID and status)
- Paused (pending approval) badge with distinct icon
- Delete button for completed/cancelled tasks
- Better empty-state guidance
"""

from datetime import datetime, timezone
from typing import Dict, List

import streamlit as st

from ui.api_client import SDLCAPIClient
from ui.config import config
from ui.state import (
    clear_auth,
    fetch_feature_flags_cached,
    fetch_tasks_cached,
)

# Status badge mapping — emoji for compact display.
STATUS_BADGES: Dict[str, str] = {
    "completed": "\u2705",  # ✅
    "planning": "\U0001f3d7\ufe0f",  # 🏗️
    "coding": "\U0001f4bb",  # 💻
    "testing": "\U0001f9ea",  # 🧪
    "reviewing": "\U0001f50d",  # 🔍
    "cancelled": "\u274c",  # ❌
    "paused": "\u23f8\ufe0f",  # ⏸️ (pending approval)
    "error": "\u26a0\ufe0f",  # ⚠️
}

# Filter categories — maps button label to status set.
FILTER_ACTIVE = {"planning", "coding", "testing", "reviewing", "paused"}
FILTER_DONE = {"completed"}
FILTER_ERROR = {"cancelled", "error"}

# Maximum requirements preview length.
PREVIEW_LEN = 60

# Maximum tasks to render (avoid sidebar overflow).
MAX_TASKS = 50


def _matches_filter(status: str, active_filter: str) -> bool:
    """Return True if a task status matches the selected filter."""
    if active_filter == "All":
        return True
    if active_filter == "Active":
        return status in FILTER_ACTIVE
    if active_filter == "Done":
        return status in FILTER_DONE
    if active_filter == "Error":
        return status in FILTER_ERROR
    return True


def _format_time_ago(created_at: str) -> str:
    """Format a datetime string as a relative time (e.g., '2h ago').

    Handles ISO 8601 strings with or without timezone info.
    Returns empty string if parsing fails.
    """
    if not created_at:
        return ""

    try:
        # Handle ISO 8601 with 'Z' suffix (UTC indicator).
        ts_str = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())

        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        if seconds < 604800:
            return f"{seconds // 86400}d ago"
        return dt.strftime("%m-%d")
    except (ValueError, TypeError):
        return ""


def _truncate(text: str, max_len: int = PREVIEW_LEN) -> str:
    """Truncate text to max_len chars, appending '...' if truncated."""
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _load_task_state(client: SDLCAPIClient, task_id: str) -> None:
    """Fetch task detail + checkpoint state, load into session_state."""
    detail = client.fetch_task_detail(task_id)
    if not detail:
        return
    thread_id = detail.get("thread_id", task_id)
    state = client.fetch_state(thread_id)
    if not state:
        return
    st.session_state.code_changes = state.get("code_changes", [])
    st.session_state.plan = state.get("plan", [])
    st.session_state.test_results = state.get("test_results", [])
    st.session_state.feedback = state.get("feedback", [])
    st.session_state.security_issues = state.get("security_issues", [])
    st.session_state.final_messages = state.get("messages", [])
    st.session_state.is_complete = state.get(
        "is_complete",
        detail.get("status") == "completed",
    )
    st.session_state.human_approval_needed = state.get("human_approval_needed", False)
    st.session_state.stream_error = None
    # Load persisted log entries + timings from state if available.
    st.session_state.log_entries = state.get("log_entries", [])
    st.session_state.node_timings = state.get("node_timings", {})
    st.rerun()


def render_session_manager():
    """Render the enhanced sidebar with session management.

    Layout:
    - Account header (user + session + server status)
    - Task count summary
    - Task list with search + filter + status badges + requirements preview
    - Feature flags (condensed)
    - Logout
    """
    with st.sidebar:
        client: SDLCAPIClient = st.session_state.api_client

        # --- Account header ---
        st.header("SDLC Orchestrator")
        user_display = client.username or "User"
        st.markdown(f":bust_in_silhouette: **{user_display}**")
        if client.session_id:
            st.caption(f"Session: `{client.session_id[:8]}...`")
        server_status = ":green_circle: Online" if st.session_state.server_online else ":red_circle: Offline"
        st.caption(f"Server: {server_status}")

        st.divider()

        # --- Task list with search + filter ---
        st.subheader("Tasks")

        col_search, col_refresh = st.columns([3, 1])
        with col_search:
            search_query = st.text_input(
                "Search",
                placeholder="Filter by ID, status, or description...",
                key="task_search",
                label_visibility="collapsed",
            )
        with col_refresh:
            if st.button(":arrows_counterclockwise:", help="Refresh tasks", use_container_width=True):
                fetch_tasks_cached(client, force=True)
                st.rerun()

        # Status filter buttons
        filter_cols = st.columns(4)
        filters = ["All", "Active", "Done", "Error"]
        for col, f in zip(filter_cols, filters):
            if col.button(
                f,
                key=f"filter_{f}",
                use_container_width=True,
                type="primary" if st.session_state.get("task_filter", "All") == f else "secondary",
            ):
                st.session_state.task_filter = f
                st.rerun()

        active_filter = st.session_state.get("task_filter", "All")

        tasks = fetch_tasks_cached(client)
        if tasks:
            # Task count summary
            _render_count_summary(tasks, active_filter)

            # Apply search + filter
            filtered = _filter_tasks(tasks, active_filter, search_query)

            if not filtered:
                st.info("No tasks match the filter.")
            else:
                for task in filtered[:MAX_TASKS]:
                    _render_task_card(client, task)
        else:
            _render_empty_state()

        st.divider()

        # --- Feature flags (condensed) ---
        with st.expander("Feature Flags", expanded=False):
            flag_values = fetch_feature_flags_cached(client)
            for flag_name, label in config.feature_flags.items():
                enabled = flag_values.get(flag_name)
                if enabled is None:
                    status = ":grey_question:"
                elif enabled:
                    status = ":white_check_mark:"
                else:
                    status = ":x:"
                st.markdown(f"{status} **{label}**")

        st.divider()

        if st.button("Logout", use_container_width=True):
            client.logout()
            clear_auth()
            st.session_state.task_results = []
            st.rerun()


def _render_count_summary(tasks: List[Dict], active_filter: str) -> None:
    """Render a one-line task count summary."""
    total = len(tasks)
    active = sum(1 for t in tasks if t.get("status", "") in FILTER_ACTIVE)
    done = sum(1 for t in tasks if t.get("status", "") in FILTER_DONE)
    error = sum(1 for t in tasks if t.get("status", "") in FILTER_ERROR)

    parts = [f"**{total}** total"]
    if active:
        parts.append(f"{active} active")
    if done:
        parts.append(f"{done} done")
    if error:
        parts.append(f"{error} error")
    st.caption(" \u00b7 ".join(parts))


def _filter_tasks(tasks: List[Dict], active_filter: str, search_query: str) -> List[Dict]:
    """Filter tasks by status filter and search query.

    Search matches task ID, status, and requirements text.
    """
    filtered = []
    q = search_query.lower().strip() if search_query else ""

    for task in tasks:
        status = task.get("status", "unknown")
        if not _matches_filter(status, active_filter):
            continue
        if q:
            task_id = task.get("id", "").lower()
            requirements = task.get("requirements", "").lower()
            if q not in task_id and q not in status.lower() and q not in requirements:
                continue
        filtered.append(task)

    return filtered


def _render_empty_state() -> None:
    """Render a helpful empty state when no tasks exist."""
    st.info("No tasks yet")
    st.caption(
        "Submit a requirement in the main area to start your first SDLC task. "
        "The agent will plan, code, test, and review — all visible here."
    )


def _render_task_card(client: SDLCAPIClient, task: dict):
    """Render a single task as a clickable card with status badge and preview.

    Layout (two rows):
    - Row 1: [Load button] [Delete button (if completed/cancelled)]
    - Row 2: requirements preview caption
    """
    status = task.get("status", "unknown")
    task_id = task.get("id", "N/A")
    requirements = task.get("requirements", "")
    created_at = task.get("created_at", "")

    badge = STATUS_BADGES.get(status, ":grey_question:")
    time_ago = _format_time_ago(created_at)
    preview = _truncate(requirements)

    # Highlight current task
    current = st.session_state.get("current_thread_id")
    is_current = current == task_id or current == task.get("thread_id")

    # Build button label: badge + id + status + time
    label = f"{badge} `{task_id[:8]}` \u00b7 {status}"
    if time_ago:
        label += f" \u00b7 {time_ago}"
    if is_current:
        label = f"\u25b6 {label}"  # ▶ current indicator

    # Row 1: load button + optional delete button
    can_delete = status in ("completed", "cancelled")
    if can_delete:
        col_load, col_del = st.columns([4, 1])
    else:
        col_load = st
        col_del = None

    with col_load:
        if st.button(
            label, key=f"task_btn_{task_id}", use_container_width=True, help=requirements if requirements else None
        ):
            _load_task_state(client, task_id)

    if col_del:
        with col_del:
            if st.button("\U0001f5d1\ufe0f", key=f"del_{task_id}", help="Delete this task", use_container_width=True):
                if client.delete_task(task_id):
                    fetch_tasks_cached(client, force=True)
                    st.rerun()

    # Row 2: requirements preview
    if preview:
        st.caption(preview)
