"""Session state management, TTL caching, and auth helpers.

Extracted from app.py to keep the orchestrator thin and let UI
components focus on rendering. All functions here are side-effect
functions that read/write ``st.session_state`` — no Streamlit widgets.
"""

import time
from typing import Any, Dict, List

import streamlit as st

from ui.api_client import SDLCAPIClient

# TTL constants (seconds) — avoid hammering the backend on every rerun.
TASKS_CACHE_TTL = 60.0
HEALTH_CHECK_TTL = 30.0
FEATURE_FLAGS_CACHE_TTL = 300.0


def init_session_state():
    """Initialize Streamlit session state variables.

    Also restores authentication tokens from the persisted auth_*
    fields into the SDLCAPIClient instance. Streamlit session_state
    survives in-tab reruns but is lost on full browser refresh; the
    auth_* fields are written by login()/register() and read back
    here so the client stays authenticated across reruns.
    """
    defaults = {
        "api_client": SDLCAPIClient(),
        "current_task_id": None,
        "task_results": [],
        "tasks_last_fetch": 0.0,
        "code_changes": [],
        "plan": [],
        "tool_calls": [],
        "human_approval_needed": False,
        "is_complete": False,
        "task_started": False,
        "stream_error": None,
        "server_online": True,
        "health_last_check": 0.0,
        "auth_access_token": None,
        "auth_session_token": None,
        "auth_session_id": None,
        "auth_username": None,
        "final_messages": [],
        "test_results": [],
        "feedback": [],
        "security_issues": [],
        "feature_flags_values": {},
        "feature_flags_last_fetch": 0.0,
        "current_thread_id": None,
        "file_reviews": {},
        "log_entries": [],
        "node_timings": {},
        "log_view_mode": "Normal",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # Restore tokens into the client instance if persisted auth exists.
    client: SDLCAPIClient = st.session_state.api_client
    if not client.is_authenticated:
        access = st.session_state.get("auth_access_token")
        session_token = st.session_state.get("auth_session_token")
        if access or session_token:
            client.restore_auth(
                access_token=access,
                session_token=session_token,
                session_id=st.session_state.get("auth_session_id"),
                username=st.session_state.get("auth_username"),
            )


def fetch_tasks_cached(client: SDLCAPIClient, force: bool = False) -> List[Dict[str, Any]]:
    """Fetch tasks with TTL caching."""
    now = time.time()
    last_fetch = st.session_state.get("tasks_last_fetch", 0.0)
    cached = st.session_state.get("task_results")

    if not force and cached is not None and (now - last_fetch) < TASKS_CACHE_TTL:
        return cached

    tasks = client.fetch_tasks()
    st.session_state.task_results = tasks
    st.session_state.tasks_last_fetch = now
    return tasks


def check_server_health_cached(client: SDLCAPIClient, force: bool = False) -> bool:
    """Check server health with TTL caching."""
    now = time.time()
    last_check = st.session_state.get("health_last_check", 0.0)

    if not force and (now - last_check) < HEALTH_CHECK_TTL:
        return st.session_state.get("server_online", True)

    online = client.check_server_health()
    st.session_state.server_online = online
    st.session_state.health_last_check = now
    return online


def fetch_feature_flags_cached(client: SDLCAPIClient, force: bool = False) -> Dict[str, bool]:
    """Fetch feature flags with TTL caching."""
    now = time.time()
    last_fetch = st.session_state.get("feature_flags_last_fetch", 0.0)
    cached = st.session_state.get("feature_flags_values")

    if not force and cached is not None and (now - last_fetch) < FEATURE_FLAGS_CACHE_TTL:
        return cached

    flags = client.fetch_feature_flags()
    st.session_state.feature_flags_values = flags
    st.session_state.feature_flags_last_fetch = now
    return flags


def apply_sdlc_result_to_session(result: Dict[str, Any]) -> None:
    """Apply an SDLCResponse dict to session_state.

    After approve/reject the backend returns the latest workflow state.
    This syncs all relevant fields so the UI reflects the new state.
    """
    st.session_state.is_complete = result.get("is_complete", False)
    st.session_state.human_approval_needed = result.get("human_approval_needed", False)
    if "plan" in result:
        st.session_state.plan = result.get("plan", [])
    if "code_changes" in result:
        st.session_state.code_changes = result.get("code_changes", [])
    if "test_results" in result:
        st.session_state.test_results = result.get("test_results", [])
    if "feedback" in result:
        st.session_state.feedback = result.get("feedback", [])
    if "security_issues" in result:
        st.session_state.security_issues = result.get("security_issues", [])
    message = result.get("message")
    if message:
        st.session_state.final_messages = [{"role": "assistant", "content": message}]
    st.session_state.stream_error = None


def persist_auth(client: SDLCAPIClient) -> None:
    """Persist auth tokens to session_state so they survive reruns."""
    st.session_state.auth_access_token = client._access_token
    st.session_state.auth_session_token = client._session_token
    st.session_state.auth_session_id = client._session_id
    st.session_state.auth_username = client._username


def clear_auth() -> None:
    """Clear persisted auth fields (called on logout)."""
    st.session_state.auth_access_token = None
    st.session_state.auth_session_token = None
    st.session_state.auth_session_id = None
    st.session_state.auth_username = None
