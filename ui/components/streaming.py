"""Real-time SSE streaming output with structured logging and stage timeline.

Two modes:
- Live streaming (task_started=True): blocking SSE loop with live
  timeline + structured log updates.
- Post-completion review (log_entries exist): static filtered log
  with view mode selector for re-reviewing the execution log.
"""

import streamlit as st

from ui.api_client import (
    AuthError,
    ConnectionError,
    SDLCAPIClient,
)
from ui.components.log_viewer import (
    append_log_entry,
    end_node_timing,
    render_log_area,
    render_log_entries,
    render_stage_timeline,
    render_view_mode_selector,
    start_node_timing,
)
from ui.state import clear_auth


def render_streaming_output():
    """Render the streaming output area.

    Live mode: blocks on the SSE event stream, updating the timeline
    and structured log in real time, then fetches the final state and
    triggers a rerun so the results panel can render.

    Review mode (task finished, log_entries persisted): shows the view
    mode selector and the filtered log for post-completion review.
    """
    entries = st.session_state.get("log_entries", [])
    task_running = st.session_state.get("task_started", False)

    # Post-completion review: show persisted log with view mode selector.
    if not task_running and entries:
        view_mode = render_view_mode_selector()
        render_log_area(view_mode)
        return

    # No task running and no log entries: nothing to render.
    if not task_running:
        return

    # --- Live streaming mode ---
    view_mode = render_view_mode_selector()
    st.subheader("Execution Log")

    timeline_placeholder = st.empty()
    log_placeholder = st.empty()

    # Verbose mode: show live LLM streaming output (token-by-token).
    stream_placeholder = st.empty() if view_mode == "Verbose" else None

    # Initial timeline render.
    _refresh_timeline(timeline_placeholder)

    client: SDLCAPIClient = st.session_state.api_client
    current_node = "system"
    stream_buffer = ""

    try:
        for event in client.stream_sdlc_task(
            st.session_state.current_requirements,
            st.session_state.current_context,
            st.session_state.current_images,
        ):
            event_type = event.get("event", "unknown")
            event_data = event.get("data", {})

            if event_type == "error":
                msg = event_data.get("error", "Unknown error")
                append_log_entry("system", msg, "error")
                st.session_state.stream_error = msg
                _refresh_log(log_placeholder, view_mode)
                break

            if event_type == "thread_started":
                thread_id = event_data.get("thread_id", "")
                st.session_state.current_thread_id = thread_id
                short_id = thread_id[:8] if thread_id else "unknown"
                append_log_entry("system", f"Thread started: {short_id}", "info")
                _refresh_log(log_placeholder, view_mode)
                continue

            if event_type == "on_chat_model_stream":
                chunk = event_data.get("chunk", {})
                content = chunk.get("content", "")
                if content:
                    stream_buffer += content
                    if stream_placeholder:
                        stream_placeholder.markdown(stream_buffer)

            elif event_type == "on_chain_start":
                name = event_data.get("name", "")
                if name:
                    current_node = name
                    start_node_timing(name)
                    append_log_entry(name, f"{name} started", "info")
                    _refresh_timeline(timeline_placeholder)
                    _refresh_log(log_placeholder, view_mode)
                    # Reset streaming buffer for the new node.
                    stream_buffer = ""
                    if stream_placeholder:
                        stream_placeholder.empty()

            elif event_type == "on_chain_end":
                name = event_data.get("name", "")
                output = event_data.get("output", {})

                # LangGraph wraps node outputs in Command objects
                # ({update: {...}, goto: ...}). Unwrap to get state delta.
                if isinstance(output, dict) and "update" in output and isinstance(output["update"], dict):
                    output = output["update"]

                if isinstance(output, dict):
                    if output.get("plan"):
                        st.session_state.plan = output["plan"]
                        append_log_entry(
                            name,
                            f"Generated plan: {len(output['plan'])} step(s)",
                            "info",
                        )
                    if output.get("code_changes"):
                        st.session_state.code_changes.extend(output["code_changes"])
                        append_log_entry(
                            name,
                            f"Code changes: {len(output['code_changes'])} file(s)",
                            "info",
                        )
                    if "human_approval_needed" in output:
                        st.session_state.human_approval_needed = output["human_approval_needed"]
                    if "is_complete" in output:
                        st.session_state.is_complete = output["is_complete"]

                if name:
                    end_node_timing(name)
                    append_log_entry(name, f"{name} completed", "success")
                    _refresh_timeline(timeline_placeholder)
                    _refresh_log(log_placeholder, view_mode)
                    stream_buffer = ""
                    if stream_placeholder:
                        stream_placeholder.empty()

            elif event_type == "on_tool_start":
                tool_name = event_data.get("name", "unknown")
                append_log_entry(current_node, f"Calling tool: {tool_name}", "tool")
                _refresh_log(log_placeholder, view_mode)

            elif event_type == "on_tool_end":
                tool_name = event_data.get("name", "unknown")
                output = event_data.get("output", "")
                output_str = str(output)[:200] if output else ""
                append_log_entry(
                    current_node,
                    f"Tool result: {tool_name} - {output_str}",
                    "tool",
                )
                _refresh_log(log_placeholder, view_mode)

        # Streaming complete.
        append_log_entry("system", "Streaming completed", "success")
        st.session_state.task_started = False
        _refresh_log(log_placeholder, view_mode)

        # Fetch the full final state from the backend checkpoint —
        # this is the source of truth for results.
        thread_id = st.session_state.get("current_thread_id") or client.session_id or ""
        if thread_id:
            final_state = client.fetch_state(thread_id)
            if final_state:
                if final_state.get("code_changes"):
                    st.session_state.code_changes = final_state["code_changes"]
                if final_state.get("plan"):
                    st.session_state.plan = final_state["plan"]
                if "is_complete" in final_state:
                    st.session_state.is_complete = final_state["is_complete"]
                if "human_approval_needed" in final_state:
                    st.session_state.human_approval_needed = final_state["human_approval_needed"]
                if final_state.get("messages"):
                    st.session_state.final_messages = final_state["messages"]
                if final_state.get("test_results"):
                    st.session_state.test_results = final_state["test_results"]
                if final_state.get("feedback"):
                    st.session_state.feedback = final_state["feedback"]
                if final_state.get("security_issues"):
                    st.session_state.security_issues = final_state["security_issues"]
            else:
                st.session_state.stream_error = (
                    "Stream finished but the task state could not be loaded from the backend checkpoint. Please retry."
                )
        st.rerun()

    except ConnectionError as e:
        st.session_state.server_online = False
        st.session_state.stream_error = str(e)
        st.session_state.task_started = False
        st.rerun()
    except AuthError as e:
        # H-1 fix: clear auth to avoid 401 death loop.
        st.session_state.stream_error = str(e)
        clear_auth()
        client.logout()
        st.session_state.task_started = False
        st.rerun()
    except Exception as e:
        st.session_state.stream_error = str(e)
        st.session_state.task_started = False
        st.rerun()


def _refresh_timeline(placeholder) -> None:
    """Re-render the stage timeline into the placeholder."""
    placeholder.empty()
    with placeholder.container():
        render_stage_timeline(st.session_state.get("node_timings", {}))


def _refresh_log(placeholder, view_mode: str) -> None:
    """Re-render filtered log entries into the placeholder."""
    entries = st.session_state.get("log_entries", [])
    placeholder.empty()
    with placeholder.container():
        render_log_entries(entries, view_mode)
