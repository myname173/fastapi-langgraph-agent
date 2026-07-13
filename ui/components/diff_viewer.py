"""Diff viewer — visual code change review with per-file markers.

Displays code_changes as color-coded file cards with syntax highlighting.
Each file can be visually marked as accepted/rejected/pending to help
the user review before approving or rejecting the whole task.

Note: per-file markers are visual aids only — the backend approve/reject
endpoints operate on the entire task. Partial approval is a future
backend enhancement.
"""

from typing import Dict, List

import streamlit as st

# Change type visual mapping.
TYPE_ICON = {
    "create": "\u2795",  # ➕
    "modify": "\u270f\ufe0f",  # ✏️
    "delete": "\u274c",  # ❌
}
TYPE_LABEL = {
    "create": "NEW",
    "modify": "MODIFIED",
    "delete": "DELETED",
}
TYPE_COLOR = {
    "create": "green",
    "modify": "orange",
    "delete": "red",
}

# Language detection from file extension.
_LANG_MAP = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "jsx": "javascript",
    "tsx": "typescript",
    "sh": "bash",
    "yml": "yaml",
    "yaml": "yaml",
    "md": "markdown",
    "html": "markup",
    "xml": "markup",
    "css": "css",
    "json": "json",
    "sql": "sql",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
}

# Review status markers.
REVIEW_ICON = {
    "accepted": "\u2705",  # ✅
    "rejected": "\u274c",  # ❌
    "pending": "\u23f8",  # ⏸
}


def _detect_language(file_path: str) -> str:
    """Detect code language from file extension."""
    ext = file_path.split(".")[-1].lower() if "." in file_path else ""
    return _LANG_MAP.get(ext, "text")


def _count_lines(content: str) -> int:
    """Count non-empty lines in content."""
    if not content:
        return 0
    return len([line for line in content.splitlines() if line.strip()])


def _get_review_status(file_path: str) -> str:
    """Get the review status of a file (default: pending)."""
    reviews: Dict[str, str] = st.session_state.setdefault("file_reviews", {})
    return reviews.get(file_path, "pending")


def _set_review_status(file_path: str, status: str) -> None:
    """Set the review status of a file."""
    reviews: Dict[str, str] = st.session_state.setdefault("file_reviews", {})
    reviews[file_path] = status


def render_diff_viewer(code_changes: List[Dict]) -> None:
    """Render the diff viewer with file cards and review markers.

    Args:
        code_changes: List of change dicts with keys:
            file_path, change_type, content, commit_message
    """
    if not code_changes:
        st.caption("No code changes.")
        return

    # --- Summary bar ---
    _render_summary(code_changes)

    # --- Review status overview ---
    _render_review_overview(code_changes)

    st.markdown("---")

    # --- File cards (sorted by path) ---
    sorted_changes = sorted(code_changes, key=lambda c: c.get("file_path", ""))
    for change in sorted_changes:
        _render_file_card(change)


def _render_summary(code_changes: List[Dict]) -> None:
    """Render a summary bar with file counts and line statistics."""
    type_counts = {"create": 0, "modify": 0, "delete": 0}
    total_lines = 0

    for change in code_changes:
        ct = change.get("change_type", change.get("action", "modify"))
        if ct in type_counts:
            type_counts[ct] += 1
        total_lines += _count_lines(change.get("content", change.get("code", "")))

    total_files = len(code_changes)

    # Build summary badges
    badges = []
    if type_counts["create"]:
        badges.append(f":green[{TYPE_ICON['create']} {type_counts['create']} new]")
    if type_counts["modify"]:
        badges.append(f":orange[{TYPE_ICON['modify']} {type_counts['modify']} modified]")
    if type_counts["delete"]:
        badges.append(f":red[{TYPE_ICON['delete']} {type_counts['delete']} deleted]")

    summary = f"**{total_files} file(s)** \u2014 " + " | ".join(badges)
    if total_lines:
        summary += f" | **{total_lines} lines**"
    st.markdown(summary)


def _render_review_overview(code_changes: List[Dict]) -> None:
    """Render the review progress overview with quick actions."""
    reviews: Dict[str, str] = st.session_state.setdefault("file_reviews", {})
    accepted = sum(1 for c in code_changes if reviews.get(c.get("file_path", c.get("path", ""))) == "accepted")
    rejected = sum(1 for c in code_changes if reviews.get(c.get("file_path", c.get("path", ""))) == "rejected")
    pending = len(code_changes) - accepted - rejected

    col_stats, col_actions = st.columns([3, 2])

    with col_stats:
        parts = []
        if accepted:
            parts.append(f"\u2705 {accepted} accepted")
        if rejected:
            parts.append(f"\u274c {rejected} rejected")
        if pending:
            parts.append(f"\u23f8 {pending} pending")
        st.caption(" | ".join(parts) if parts else "Review files below")

    with col_actions:
        col_a, col_r = st.columns(2)
        with col_a:
            if st.button("Accept All", key="accept_all", use_container_width=True, help="Mark all files as accepted"):
                for c in code_changes:
                    reviews[c.get("file_path", "")] = "accepted"
                st.rerun()
        with col_r:
            if st.button("Reset", key="reset_reviews", use_container_width=True, help="Reset all review markers"):
                st.session_state.file_reviews = {}
                st.rerun()


def _render_file_card(change: Dict) -> None:
    """Render a single file change as a card with review markers."""
    # Handle both new and legacy field names (file_path/path, content/code).
    file_path = change.get("file_path", change.get("path", "unknown"))
    change_type = change.get("change_type", change.get("action", "modify"))
    content = change.get("content", change.get("code", ""))
    commit_msg = change.get("commit_message", "")

    icon = TYPE_ICON.get(change_type, "\U0001f4c1")
    label = TYPE_LABEL.get(change_type, "CHANGE")
    line_count = _count_lines(content)
    review_status = _get_review_status(file_path)

    # Header row: icon + label + file path + line count + review status
    header = f"{icon} **{label}** \u2014 `{file_path}`"
    if line_count:
        header += f" ({line_count} lines)"
    if review_status != "pending":
        header += f" {REVIEW_ICON[review_status]}"

    with st.container(border=True):
        st.markdown(header)

        # Commit message / description
        if commit_msg:
            st.caption(f"\U0001f4dd {commit_msg}")

        # Code content
        if content:
            lang = _detect_language(file_path)
            st.code(content, language=lang)

            # Download button
            filename = file_path.split("/")[-1]
            st.download_button(
                label=f"Download {filename}",
                data=content.encode("utf-8"),
                file_name=filename,
                mime="text/plain",
                key=f"dl_{file_path}",
            )
        elif change_type == "delete":
            st.error("File will be deleted.")
        else:
            st.info("Change applied via smart edit (in-place modification). Content not shown.")

        # Review marker buttons
        col_a, col_r, col_p = st.columns(3)
        with col_a:
            if st.button(
                "\u2705 Accept",
                key=f"accept_{file_path}",
                use_container_width=True,
                type="primary" if review_status == "accepted" else "secondary",
            ):
                _set_review_status(file_path, "accepted")
                st.rerun()
        with col_r:
            if st.button(
                "\u274c Reject",
                key=f"reject_{file_path}",
                use_container_width=True,
                type="primary" if review_status == "rejected" else "secondary",
            ):
                _set_review_status(file_path, "rejected")
                st.rerun()
        with col_p:
            if st.button(
                "\u23f8 Pending",
                key=f"pending_{file_path}",
                use_container_width=True,
                type="primary" if review_status == "pending" else "secondary",
            ):
                _set_review_status(file_path, "pending")
                st.rerun()


def get_review_summary(code_changes: List[Dict]) -> Dict[str, int]:
    """Get a summary of review statuses for the approval workflow.

    Returns:
        Dict with keys: accepted, rejected, pending, total
    """
    reviews: Dict[str, str] = st.session_state.get("file_reviews", {})
    accepted = sum(1 for c in code_changes if reviews.get(c.get("file_path", c.get("path", ""))) == "accepted")
    rejected = sum(1 for c in code_changes if reviews.get(c.get("file_path", c.get("path", ""))) == "rejected")
    total = len(code_changes)
    pending = total - accepted - rejected
    return {"accepted": accepted, "rejected": rejected, "pending": pending, "total": total}
