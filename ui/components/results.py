"""Results panel — plan, code changes, tests, feedback, security, approval.

Displayed in the right column of the three-column layout. Uses
sub-tabs so the user can switch between result categories without
scrolling through a long vertical list.
"""

from typing import Any

import streamlit as st

from ui.api_client import SDLCAPIClient
from ui.components.diff_viewer import get_review_summary, render_diff_viewer
from ui.components.preview import render_preview_tab
from ui.state import apply_sdlc_result_to_session


def _extract_message_content(msg: Any) -> str:
    """Extract text content from a message dict or LangChain BaseMessage."""
    if isinstance(msg, dict):
        return str(msg.get("content", ""))
    content = getattr(msg, "content", None)
    if content is not None:
        return str(content)
    return str(msg)


def render_results():
    """Render the task results panel.

    Shows stream errors first, then results when completed or when
    human approval is needed. Uses sub-tabs for category navigation.
    """
    if st.session_state.get("stream_error"):
        render_error_with_retry()
        return

    if not (st.session_state.get("is_complete") or st.session_state.get("human_approval_needed")):
        _render_placeholder()
        return

    st.subheader("Results")

    # Sub-tabs for result categories
    tab_plan, tab_code, tab_tests, tab_review, tab_preview = st.tabs(["Plan", "Code", "Tests", "Review", "Preview"])

    with tab_plan:
        _render_plan()

    with tab_code:
        _render_code_changes()

    with tab_tests:
        _render_test_results()

    with tab_review:
        _render_feedback()
        _render_security_issues()
        _render_final_messages()

    with tab_preview:
        render_preview_tab()

    # Approval workflow at the bottom
    _render_approval()


def _render_placeholder():
    """Show a friendly placeholder when no results are available yet."""
    if st.session_state.get("task_started"):
        st.info(":hourglass: Task is running. Results will appear here when complete.")
    else:
        st.caption("Submit a task to see results here.")


def _render_plan():
    """Render the execution plan."""
    plan = st.session_state.get("plan")
    if not plan:
        st.caption("No plan generated.")
        return

    if isinstance(plan, list):
        for i, task in enumerate(plan):
            if isinstance(task, dict):
                desc = task.get("description", task.get("task", str(task)))
                status = task.get("status", "pending")
                emoji = ":white_check_mark:" if status == "completed" else ":hourglass:"
                st.markdown(f"{emoji} **Task {i + 1}:** {desc}")


def _render_code_changes():
    """Render code changes via the diff viewer component.

    Delegates to ``render_diff_viewer`` which provides per-file cards,
    syntax highlighting, review markers, and a change summary.
    """
    changes = st.session_state.get("code_changes")
    if not changes or not isinstance(changes, list):
        st.caption("No code changes.")
        return
    render_diff_viewer(changes)


def _render_test_results():
    """Render test results."""
    tests = st.session_state.get("test_results")
    if not tests:
        st.caption("No test results.")
        return

    if not isinstance(tests, list):
        return

    for i, test in enumerate(tests):
        if not isinstance(test, dict):
            continue
        passed = test.get("passed", test.get("status") == "pass")
        icon = ":white_check_mark:" if passed else ":x:"
        name = test.get("test_name", test.get("name", f"Test {i + 1}"))
        with st.expander(f"{icon} {name}", expanded=False):
            if test.get("output"):
                st.code(str(test["output"]), language="text")
            if test.get("error_message"):
                st.error(str(test["error_message"]))
            elif test.get("error"):
                st.error(str(test["error"]))


def _render_feedback():
    """Render agent feedback."""
    feedback = st.session_state.get("feedback")
    if not feedback:
        return

    st.markdown("**Feedback**")
    if isinstance(feedback, list):
        for fb in feedback:
            if isinstance(fb, dict):
                agent = fb.get("agent_name", fb.get("agent", "Agent"))
                severity = fb.get("severity", "")
                severity_tag = f" `[{severity}]`" if severity else ""
                st.info(f"**{agent}:**{severity_tag} {fb.get('content', str(fb))}")
            else:
                st.info(str(fb))


def _render_security_issues():
    """Render security issues."""
    issues = st.session_state.get("security_issues")
    if not issues:
        return

    st.markdown("**Security Issues**")
    if not isinstance(issues, list):
        return

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = issue.get("severity", "medium")
        icon = {
            "critical": ":rotating_light:",
            "high": ":warning:",
            "medium": ":eyes:",
            "low": ":information_source:",
        }.get(severity, ":eyes:")
        msg = issue.get("message", str(issue))
        file = issue.get("file", "")
        file_tag = f" (`{file}`)" if file else ""
        with st.expander(f"{icon} [{severity.upper()}] {msg[:80]}{file_tag}", expanded=False):
            st.write(f"**Severity:** {severity}")
            if file:
                st.write(f"**File:** `{file}`")
            st.write(f"**Message:** {msg}")
            if issue.get("match"):
                st.code(str(issue["match"]), language="text")
            if issue.get("context"):
                st.code(str(issue["context"]), language="text")


def _render_final_messages():
    """Render agent summary messages."""
    final_msgs = st.session_state.get("final_messages", [])
    if not final_msgs:
        return

    st.markdown("**Agent Summary**")
    if isinstance(final_msgs, list):
        recent = final_msgs[-3:] if len(final_msgs) > 3 else final_msgs
        for msg in recent:
            content = _extract_message_content(msg)
            if not content or not content.strip():
                continue
            role = "assistant"
            if isinstance(msg, dict):
                role = msg.get("role", "assistant")
            elif hasattr(msg, "type"):
                role = msg.type
            if role in ("assistant", "ai", "AIMessage", "AIMessageChunk"):
                st.markdown(content)
            elif role in ("user", "human", "HumanMessage"):
                st.info(f"**You:** {content}")
            else:
                st.markdown(content)


def _render_approval():
    """Render the human approval workflow with comprehensive summary.

    Phase 5 enhancements:
    - Metrics row: Files / Lines / Tests / Security at a glance
    - Detailed breakdown: file types, test pass/fail, security severity
    - Reject method selection: modify (coder fix) vs replan (restart planner)
    - Per-file review status from the diff viewer
    """
    client: SDLCAPIClient = st.session_state.api_client

    if not st.session_state.get("human_approval_needed"):
        has_output = any(
            [
                st.session_state.get("plan"),
                st.session_state.get("code_changes"),
                st.session_state.get("test_results"),
                st.session_state.get("feedback"),
                st.session_state.get("final_messages"),
            ]
        )
        if has_output:
            st.success("Task completed!")
        return

    st.subheader("Human Approval Required")
    st.warning("This task requires your approval before proceeding.")

    changes = st.session_state.get("code_changes", [])
    tests = st.session_state.get("test_results", [])
    security = st.session_state.get("security_issues", [])

    # --- Metrics row: quick at-a-glance summary ---
    col_files, col_lines, col_tests, col_sec = st.columns(4)

    file_count = len(changes) if isinstance(changes, list) else 0
    line_count = _count_total_lines(changes)
    passed, failed, skipped = _summarize_tests(tests)
    test_total = passed + failed + skipped
    sec_count = len(security) if isinstance(security, list) else 0

    with col_files:
        st.metric("Files", file_count)
    with col_lines:
        st.metric("Lines", line_count)
    with col_tests:
        if test_total == 0:
            st.metric("Tests", "\u2014")
        else:
            st.metric("Tests", f"{passed}/{test_total}")
    with col_sec:
        st.metric("Security", "\u2705 Clean" if sec_count == 0 else f"{sec_count} issues")

    # --- Detailed breakdown (collapsible) ---
    with st.expander("Detailed Summary", expanded=True):
        _render_change_breakdown(changes)
        _render_test_breakdown(passed, failed, skipped)
        _render_security_breakdown(security)

    # --- Per-file review status ---
    if changes and isinstance(changes, list):
        review = get_review_summary(changes)
        col_r, col_f = st.columns([2, 3])

        with col_r:
            st.markdown("**Review Status**")
            parts = []
            if review["accepted"]:
                parts.append(f"\u2705 {review['accepted']} accepted")
            if review["rejected"]:
                parts.append(f"\u274c {review['rejected']} rejected")
            if review["pending"]:
                parts.append(f"\u23f8 {review['pending']} pending")
            st.markdown(" | ".join(parts) if parts else "No files reviewed yet")

        with col_f:
            st.markdown("**Change Types**")
            type_counts = {}
            for c in changes:
                ct = c.get("change_type", c.get("action", "modify"))
                type_counts[ct] = type_counts.get(ct, 0) + 1
            type_parts = [f"{ct}: {n}" for ct, n in type_counts.items()]
            st.markdown(", ".join(type_parts) if type_parts else "none")

        # List rejected files as a warning
        reviews: dict = st.session_state.get("file_reviews", {})
        rejected_files = [
            c.get("file_path", c.get("path", "?"))
            for c in changes
            if reviews.get(c.get("file_path", c.get("path", ""))) == "rejected"
        ]
        if rejected_files:
            st.warning(f"Rejected files: {', '.join(rejected_files[:5])}")

    # --- Reject method selection ---
    reject_method = st.radio(
        "Reject Action",
        ["modify", "replan"],
        horizontal=True,
        format_func=lambda x: f"{x} \u2014 {'ask coder to fix' if x == 'modify' else 'restart from planner'}",
        key="reject_method",
        help="Controls how the workflow resumes after rejection: modify sends the task back to the coder, replan restarts from the planner.",
    )

    # --- Comment / rejection reason ---
    comment = st.text_area(
        "Comment (optional)",
        placeholder="Add a note for the reviewer agent, or explain why you're rejecting...",
        height=60,
        key="approval_comment",
    )

    col1, col2 = st.columns(2)

    with col1:
        if st.button("\u2705 Approve", type="primary", use_container_width=True):
            thread_id = st.session_state.get("current_thread_id") or client.session_id or ""
            result = client.approve_task(thread_id)
            if result:
                st.success("Task approved!")
                st.session_state.file_reviews = {}
                apply_sdlc_result_to_session(result)
                st.rerun()

    with col2:
        reject_label = "\u274c Reject"
        if st.button(reject_label, use_container_width=True):
            thread_id = st.session_state.get("current_thread_id") or client.session_id or ""
            reason = comment.strip() if comment.strip() else f"Rejected: {reject_method}"
            result = client.reject_task(thread_id, reason)
            if result:
                st.info("Task rejected.")
                st.session_state.file_reviews = {}
                apply_sdlc_result_to_session(result)
                st.rerun()


def _count_total_lines(changes) -> int:
    """Count non-empty lines across all code changes."""
    if not changes or not isinstance(changes, list):
        return 0
    total = 0
    for c in changes:
        if not isinstance(c, dict):
            continue
        content = c.get("content", c.get("code", ""))
        if content:
            total += len([line for line in content.splitlines() if line.strip()])
    return total


def _summarize_tests(tests) -> tuple:
    """Summarize test results into (passed, failed, skipped) counts.

    Handles both ``status`` string ("pass"/"fail"/"skip") and legacy
    ``passed`` boolean fields.
    """
    if not tests or not isinstance(tests, list):
        return 0, 0, 0
    passed = failed = skipped = 0
    for test in tests:
        if not isinstance(test, dict):
            continue
        status = test.get("status")
        if status == "pass":
            passed += 1
        elif status == "fail":
            failed += 1
        elif status == "skip":
            skipped += 1
        elif test.get("passed") is True:
            passed += 1
        elif test.get("passed") is False:
            failed += 1
    return passed, failed, skipped


def _render_change_breakdown(changes) -> None:
    """Render file change type breakdown."""
    if not changes or not isinstance(changes, list):
        st.markdown("**Files:** none")
        return
    type_counts = {"create": 0, "modify": 0, "delete": 0}
    for c in changes:
        if not isinstance(c, dict):
            continue
        ct = c.get("change_type", c.get("action", "modify"))
        if ct in type_counts:
            type_counts[ct] += 1
    parts = [
        f"{type_counts['create']} new",
        f"{type_counts['modify']} modified",
        f"{type_counts['delete']} deleted",
    ]
    st.markdown(f"**Files:** {len(changes)} total \u2014 {', '.join(parts)}")


def _render_test_breakdown(passed: int, failed: int, skipped: int) -> None:
    """Render test results breakdown."""
    total = passed + failed + skipped
    if total == 0:
        st.markdown("**Tests:** not run")
        return
    parts = [f"{passed} passed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    icon = "\u2705" if failed == 0 else "\u26a0\ufe0f"
    st.markdown(f"**Tests:** {icon} {', '.join(parts)}")


def _render_security_breakdown(security) -> None:
    """Render security issues breakdown by severity."""
    if not security or not isinstance(security, list) or len(security) == 0:
        st.markdown("**Security:** \u2705 No issues found")
        return
    severity_order = ["critical", "high", "medium", "low"]
    severity_counts = {}
    for issue in security:
        if not isinstance(issue, dict):
            continue
        sev = issue.get("severity", "medium")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    parts = []
    for sev in severity_order:
        if sev in severity_counts:
            parts.append(f"{sev}: {severity_counts[sev]}")
    icon = "\U0001f6a8" if severity_counts.get("critical", 0) > 0 else "\u26a0\ufe0f"
    st.markdown(f"**Security:** {icon} {len(security)} issue(s) \u2014 {', '.join(parts)}")


def render_error_with_retry():
    """Render error message with retry button."""
    error = st.session_state.get("stream_error")
    if not error:
        return

    st.error(f"Error: {error}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Retry", type="primary", use_container_width=True):
            st.session_state.stream_error = None
            st.session_state.task_started = True
            st.rerun()
    with col2:
        if st.button("Dismiss", use_container_width=True):
            st.session_state.stream_error = None
            st.rerun()
