"""UI logic tests with mock data — no API quota consumed.

Tests are split into three tiers:
1. Main tests: pure logic functions (no Streamlit dependency)
2. State tests: functions using st.session_state (mocked as dict)
3. Render tests: functions calling st.markdown/st.caption (mocked to
   capture output) — secondary priority

Run inside the UI container:
    docker compose exec ui /app/.venv/bin/python ui/tests/test_ui_logic.py
Or with pytest:
    docker compose exec ui /app/.venv/bin/python -m pytest ui/tests/test_ui_logic.py -v
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from unittest.mock import patch, MagicMock


# ============================================================================
# Mock Data Fixtures
# ============================================================================

MOCK_CODE_CHANGES: List[Dict] = [
    {
        "file_path": "app/api/auth.py",
        "change_type": "modify",
        "content": 'import jwt\n\ndef login():\n    token = jwt.encode({"user": 1}, "secret")\n    return token\n',
    },
    {
        "file_path": "app/utils/helper.py",
        "change_type": "create",
        "content": "def helper():\n    return True\n",
    },
    {
        "file_path": "app/old/deprecated.py",
        "change_type": "delete",
        "content": "",
    },
]

MOCK_TEST_RESULTS: List[Dict] = [
    {"test_name": "test_login", "status": "pass", "duration_ms": 120},
    {"test_name": "test_logout", "status": "pass", "duration_ms": 50},
    {"test_name": "test_auth_fail", "status": "fail", "error_message": "AssertionError"},
    {"test_name": "test_skip_case", "status": "skip"},
]

# Legacy-format test results (boolean "passed" field instead of status string)
MOCK_TEST_RESULTS_LEGACY: List[Dict] = [
    {"test_name": "test_a", "passed": True},
    {"test_name": "test_b", "passed": False},
]

MOCK_SECURITY_ISSUES: List[Dict] = [
    {"severity": "high", "message": "SQL injection in query", "file": "db.py"},
    {"severity": "medium", "message": "Weak password hash", "file": "auth.py"},
]

MOCK_TASKS: List[Dict] = [
    {
        "id": "abc12345-def67890-aaa11111-bbb22222",
        "status": "completed",
        "requirements": "Build a login page with JWT authentication",
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    },
    {
        "id": "def67890-abc12345-ccc33333-ddd44444",
        "status": "coding",
        "requirements": "Fix the database connection pool bug",
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
    },
    {
        "id": "ghi11111-jkl22222-eee55555-fff66666",
        "status": "paused",
        "requirements": "Add rate limiting to the API endpoints",
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    },
    {
        "id": "mno33333-pqr44444-ggg77777-hhh88888",
        "status": "cancelled",
        "requirements": "Old deprecated task to remove",
        "created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    },
]

MOCK_LOG_ENTRIES: List[Dict] = [
    {"timestamp": "10:32:01", "node": "system", "level": "info", "message": "Thread started: abc12345"},
    {"timestamp": "10:32:02", "node": "planner", "level": "info", "message": "planner started"},
    {"timestamp": "10:32:03", "node": "planner", "level": "info", "message": "Generated plan: 3 step(s)"},
    {"timestamp": "10:32:04", "node": "planner", "level": "success", "message": "planner completed"},
    {"timestamp": "10:32:05", "node": "coder", "level": "info", "message": "coder started"},
    {"timestamp": "10:32:06", "node": "coder", "level": "tool", "message": "Calling tool: write_file"},
    {"timestamp": "10:32:07", "node": "coder", "level": "debug", "message": "streaming chunk content..."},
    {"timestamp": "10:32:08", "node": "coder", "level": "tool", "message": "Tool result: write_file - OK"},
    {"timestamp": "10:32:09", "node": "coder", "level": "success", "message": "coder completed"},
    {"timestamp": "10:32:10", "node": "tester", "level": "error", "message": "test_auth_fail failed"},
]


# ============================================================================
# Tier 1: Main Tests (pure logic, no Streamlit dependency)
# ============================================================================


def test_count_total_lines():
    """Test _count_total_lines counts non-empty lines correctly."""
    from ui.components.results import _count_total_lines

    assert _count_total_lines([]) == 0
    assert _count_total_lines(None) == 0
    assert _count_total_lines([{"content": "a\nb\nc"}]) == 3
    assert _count_total_lines([{"content": "a\n\nb\n  \nc"}]) == 3  # blank lines excluded
    assert _count_total_lines(MOCK_CODE_CHANGES) == 6  # 4 + 2 + 0
    # Legacy field name "code" instead of "content"
    assert _count_total_lines([{"code": "x\ny"}]) == 2
    print("  PASS: _count_total_lines")


def test_summarize_tests():
    """Test _summarize_tests handles status strings and legacy booleans."""
    from ui.components.results import _summarize_tests

    assert _summarize_tests([]) == (0, 0, 0)
    assert _summarize_tests(None) == (0, 0, 0)
    passed, failed, skipped = _summarize_tests(MOCK_TEST_RESULTS)
    assert passed == 2, f"expected 2 passed, got {passed}"
    assert failed == 1, f"expected 1 failed, got {failed}"
    assert skipped == 1, f"expected 1 skipped, got {skipped}"
    # Legacy format
    p, f, s = _summarize_tests(MOCK_TEST_RESULTS_LEGACY)
    assert p == 1 and f == 1 and s == 0
    print("  PASS: _summarize_tests")


def test_filter_tasks_by_status():
    """Test _filter_tasks filters by status category."""
    from ui.components.session_manager import _filter_tasks

    # All filter
    assert len(_filter_tasks(MOCK_TASKS, "All", "")) == 4
    # Active filter (planning/coding/testing/reviewing/paused)
    active = _filter_tasks(MOCK_TASKS, "Active", "")
    assert len(active) == 2  # coding + paused
    # Done filter
    done = _filter_tasks(MOCK_TASKS, "Done", "")
    assert len(done) == 1  # completed
    # Error filter
    error = _filter_tasks(MOCK_TASKS, "Error", "")
    assert len(error) == 1  # cancelled
    print("  PASS: _filter_tasks (status)")


def test_filter_tasks_by_search():
    """Test _filter_tasks searches ID, status, and requirements."""
    from ui.components.session_manager import _filter_tasks

    # Search by ID fragment (abc12345 appears in 2 task IDs)
    assert len(_filter_tasks(MOCK_TASKS, "All", "abc12345")) == 2
    # Unique ID fragment
    assert len(_filter_tasks(MOCK_TASKS, "All", "ghi11111")) == 1
    # Search by requirements text
    results = _filter_tasks(MOCK_TASKS, "All", "login")
    assert len(results) == 1
    assert results[0]["requirements"] == "Build a login page with JWT authentication"
    # Search by status
    results = _filter_tasks(MOCK_TASKS, "All", "paused")
    assert len(results) == 1
    # No match
    assert len(_filter_tasks(MOCK_TASKS, "All", "nonexistent_query")) == 0
    # Combined filter + search
    results = _filter_tasks(MOCK_TASKS, "Active", "database")
    assert len(results) == 1  # coding task with "database" in requirements
    print("  PASS: _filter_tasks (search)")


def test_format_time_ago():
    """Test _format_time_ago produces correct relative time strings."""
    from ui.components.session_manager import _format_time_ago

    now = datetime.now(timezone.utc)
    # Just now (< 60s)
    assert _format_time_ago(now.isoformat()) == "just now"
    # Minutes ago
    assert _format_time_ago((now - timedelta(minutes=30)).isoformat()) == "30m ago"
    # Hours ago
    assert _format_time_ago((now - timedelta(hours=2)).isoformat()) == "2h ago"
    # Days ago
    assert _format_time_ago((now - timedelta(days=3)).isoformat()) == "3d ago"
    # Empty string
    assert _format_time_ago("") == ""
    # Invalid input
    assert _format_time_ago("not-a-date") == ""
    print("  PASS: _format_time_ago")


def test_truncate():
    """Test _truncate truncates long text and preserves short text."""
    from ui.components.session_manager import _truncate, PREVIEW_LEN

    assert _truncate("") == ""
    assert _truncate("short") == "short"
    assert _truncate("a" * 100) == "a" * PREVIEW_LEN + "..."
    # Newlines are replaced with spaces
    assert _truncate("line1\nline2") == "line1 line2"
    print("  PASS: _truncate")


def test_matches_filter():
    """Test _matches_filter correctly categorizes statuses."""
    from ui.components.session_manager import _matches_filter

    assert _matches_filter("completed", "All") is True
    assert _matches_filter("completed", "Done") is True
    assert _matches_filter("completed", "Active") is False
    assert _matches_filter("coding", "Active") is True
    assert _matches_filter("paused", "Active") is True
    assert _matches_filter("cancelled", "Error") is True
    assert _matches_filter("planning", "Error") is False
    print("  PASS: _matches_filter")


def test_view_mode_levels():
    """Test VIEW_MODE_LEVELS filtering logic."""
    from ui.components.log_viewer import VIEW_MODE_LEVELS, DEFAULT_VIEW_MODE

    assert DEFAULT_VIEW_MODE == "Normal"
    # Verbose includes all levels
    assert "debug" in VIEW_MODE_LEVELS["Verbose"]
    assert "tool" in VIEW_MODE_LEVELS["Verbose"]
    # Normal excludes debug
    assert "debug" not in VIEW_MODE_LEVELS["Normal"]
    assert "info" in VIEW_MODE_LEVELS["Normal"]
    assert "tool" in VIEW_MODE_LEVELS["Normal"]
    # Summary only has success/warning/error
    assert "info" not in VIEW_MODE_LEVELS["Summary"]
    assert "tool" not in VIEW_MODE_LEVELS["Summary"]
    assert "success" in VIEW_MODE_LEVELS["Summary"]
    assert "error" in VIEW_MODE_LEVELS["Summary"]

    # Verify filtering on mock entries
    verbose_count = sum(1 for e in MOCK_LOG_ENTRIES if e["level"] in VIEW_MODE_LEVELS["Verbose"])
    normal_count = sum(1 for e in MOCK_LOG_ENTRIES if e["level"] in VIEW_MODE_LEVELS["Normal"])
    summary_count = sum(1 for e in MOCK_LOG_ENTRIES if e["level"] in VIEW_MODE_LEVELS["Summary"])
    assert verbose_count == 10  # all
    assert normal_count == 9  # excludes 1 debug
    assert summary_count == 3  # 2 success + 1 error
    print("  PASS: VIEW_MODE_LEVELS")


def test_detect_language():
    """Test _detect_language maps file extensions to language names."""
    from ui.components.diff_viewer import _detect_language

    assert _detect_language("app/main.py") == "python"
    assert _detect_language("app/main.js") == "javascript"
    assert _detect_language("app/main.ts") == "typescript"
    assert _detect_language("app/main.tsx") == "typescript"
    assert _detect_language("config.yml") == "yaml"
    assert _detect_language("config.yaml") == "yaml"
    assert _detect_language("README.md") == "markdown"
    assert _detect_language("Dockerfile") == "text"  # no extension
    assert _detect_language("app/main.unknown") == "text"  # unknown ext
    print("  PASS: _detect_language")


def test_count_lines():
    """Test _count_lines counts non-empty lines."""
    from ui.components.diff_viewer import _count_lines

    assert _count_lines("") == 0
    assert _count_lines(None) == 0
    assert _count_lines("one") == 1
    assert _count_lines("one\ntwo\nthree") == 3
    assert _count_lines("one\n\n\ntwo") == 2  # blank lines excluded
    print("  PASS: _count_lines")


# ============================================================================
# Tier 2: State Tests (mocked st.session_state as dict)
# ============================================================================


def test_append_log_entry():
    """Test append_log_entry creates structured entries in session_state."""
    from ui.components.log_viewer import append_log_entry

    mock_state: Dict = {}
    with patch("streamlit.session_state", mock_state):
        append_log_entry("planner", "planner started", "info")
        append_log_entry("coder", "coder completed", "success")

    assert "log_entries" in mock_state
    entries = mock_state["log_entries"]
    assert len(entries) == 2
    assert entries[0]["node"] == "planner"
    assert entries[0]["level"] == "info"
    assert entries[0]["message"] == "planner started"
    assert "timestamp" in entries[0]
    assert entries[1]["node"] == "coder"
    assert entries[1]["level"] == "success"
    print("  PASS: append_log_entry")


def test_node_timing():
    """Test start_node_timing and end_node_timing track duration."""
    from ui.components.log_viewer import start_node_timing, end_node_timing

    mock_state: Dict = {}
    with patch("streamlit.session_state", mock_state):
        start_node_timing("planner")
        assert mock_state["node_timings"]["planner"]["status"] == "running"
        assert mock_state["node_timings"]["planner"]["duration_s"] is None

        time.sleep(0.1)  # simulate work
        end_node_timing("planner")
        assert mock_state["node_timings"]["planner"]["status"] == "completed"
        assert mock_state["node_timings"]["planner"]["duration_s"] is not None
        assert mock_state["node_timings"]["planner"]["duration_s"] >= 0.1

        # End timing for a node that wasn't started (should not crash)
        end_node_timing("tester")
        assert "tester" not in mock_state["node_timings"]
    print("  PASS: start_node_timing / end_node_timing")


def test_review_status_get_set():
    """Test _get_review_status and _set_review_status with mocked state."""
    from ui.components.diff_viewer import _get_review_status, _set_review_status

    mock_state: Dict = {}
    with patch("streamlit.session_state", mock_state):
        # Default status is pending
        assert _get_review_status("app/api/auth.py") == "pending"

        _set_review_status("app/api/auth.py", "accepted")
        assert _get_review_status("app/api/auth.py") == "accepted"

        _set_review_status("app/utils/helper.py", "rejected")
        assert _get_review_status("app/utils/helper.py") == "rejected"
        assert _get_review_status("app/other.py") == "pending"
    print("  PASS: _get_review_status / _set_review_status")


def test_get_review_summary():
    """Test get_review_summary counts accepted/rejected/pending correctly."""
    from ui.components.diff_viewer import get_review_summary

    mock_state: Dict = {
        "file_reviews": {
            "app/api/auth.py": "accepted",
            "app/utils/helper.py": "rejected",
            # app/old/deprecated.py left as pending
        }
    }
    with patch("streamlit.session_state", mock_state):
        summary = get_review_summary(MOCK_CODE_CHANGES)

    assert summary["total"] == 3
    assert summary["accepted"] == 1
    assert summary["rejected"] == 1
    assert summary["pending"] == 1
    print("  PASS: get_review_summary")


# ============================================================================
# Tier 3: Render Tests (mocked st.markdown/st.caption — secondary)
# ============================================================================


def test_render_log_entries_verbose():
    """Test render_log_entries shows all entries in Verbose mode."""
    from ui.components.log_viewer import render_log_entries

    mock_md = MagicMock()
    mock_caption = MagicMock()
    with patch("streamlit.markdown", mock_md), patch("streamlit.caption", mock_caption):
        render_log_entries(MOCK_LOG_ENTRIES, "Verbose")

    # Should call st.markdown once with all 10 entries
    mock_md.assert_called_once()
    rendered = mock_md.call_args[0][0]
    assert "planner started" in rendered
    assert "streaming chunk" in rendered  # debug entry visible in Verbose
    assert "Tool result" in rendered  # tool entry visible
    print("  PASS: render_log_entries (Verbose)")


def test_render_log_entries_summary():
    """Test render_log_entries hides debug/info/tool in Summary mode."""
    from ui.components.log_viewer import render_log_entries

    mock_md = MagicMock()
    mock_caption = MagicMock()
    with patch("streamlit.markdown", mock_md), patch("streamlit.caption", mock_caption):
        render_log_entries(MOCK_LOG_ENTRIES, "Summary")

    mock_md.assert_called_once()
    rendered = mock_md.call_args[0][0]
    assert "planner completed" in rendered  # success visible
    assert "test_auth_fail failed" in rendered  # error visible
    assert "planner started" not in rendered  # info hidden
    assert "streaming chunk" not in rendered  # debug hidden
    assert "Calling tool" not in rendered  # tool hidden
    print("  PASS: render_log_entries (Summary)")


def test_render_stage_timeline():
    """Test render_stage_timeline shows status icons and durations."""
    from ui.components.log_viewer import render_stage_timeline

    timings = {
        "planner": {"status": "completed", "duration_s": 2.1},
        "coder": {"status": "running", "duration_s": None},
    }
    mock_md = MagicMock()
    with patch("streamlit.markdown", mock_md):
        render_stage_timeline(timings)

    mock_md.assert_called_once()
    rendered = mock_md.call_args[0][0]
    assert "Planner" in rendered
    assert "2.1s" in rendered
    assert "Coder" in rendered
    assert "running" in rendered
    # Pending stages
    assert "Tester" in rendered
    assert "Reviewer" in rendered
    print("  PASS: render_stage_timeline")


def test_render_change_breakdown():
    """Test _render_change_breakdown shows file type counts."""
    from ui.components.results import _render_change_breakdown

    mock_md = MagicMock()
    with patch("streamlit.markdown", mock_md):
        _render_change_breakdown(MOCK_CODE_CHANGES)

    mock_md.assert_called_once()
    rendered = mock_md.call_args[0][0]
    assert "3 total" in rendered
    assert "1 new" in rendered
    assert "1 modified" in rendered
    assert "1 deleted" in rendered
    print("  PASS: _render_change_breakdown")


def test_render_test_breakdown():
    """Test _render_test_breakdown shows pass/fail/skip counts."""
    from ui.components.results import _render_test_breakdown

    mock_md = MagicMock()
    with patch("streamlit.markdown", mock_md):
        _render_test_breakdown(passed=2, failed=1, skipped=1)

    mock_md.assert_called_once()
    rendered = mock_md.call_args[0][0]
    assert "2 passed" in rendered
    assert "1 failed" in rendered
    assert "1 skipped" in rendered
    print("  PASS: _render_test_breakdown")


def test_render_security_breakdown():
    """Test _render_security_breakdown shows severity counts."""
    from ui.components.results import _render_security_breakdown

    mock_md = MagicMock()
    with patch("streamlit.markdown", mock_md):
        _render_security_breakdown(MOCK_SECURITY_ISSUES)

    mock_md.assert_called_once()
    rendered = mock_md.call_args[0][0]
    assert "2 issue" in rendered
    assert "high: 1" in rendered
    assert "medium: 1" in rendered

    # Empty security issues
    with patch("streamlit.markdown", mock_md):
        _render_security_breakdown([])
    rendered = mock_md.call_args[0][0]
    assert "No issues found" in rendered
    print("  PASS: _render_security_breakdown")


# ============================================================================
# Test Runner
# ============================================================================

# All test functions grouped by tier.
MAIN_TESTS = [
    test_count_total_lines,
    test_summarize_tests,
    test_filter_tasks_by_status,
    test_filter_tasks_by_search,
    test_format_time_ago,
    test_truncate,
    test_matches_filter,
    test_view_mode_levels,
    test_detect_language,
    test_count_lines,
]

STATE_TESTS = [
    test_append_log_entry,
    test_node_timing,
    test_review_status_get_set,
    test_get_review_summary,
]

RENDER_TESTS = [
    test_render_log_entries_verbose,
    test_render_log_entries_summary,
    test_render_stage_timeline,
    test_render_change_breakdown,
    test_render_test_breakdown,
    test_render_security_breakdown,
]


def run_tier(name: str, tests: list) -> tuple:
    """Run a tier of tests, return (passed, failed)."""
    print(f"\n{'=' * 60}")
    print(f"  {name} ({len(tests)} tests)")
    print(f"{'=' * 60}")

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL: {test.__name__} — {e}")

    print(f"  --- {name}: {passed} passed, {failed} failed ---")
    return passed, failed


def main():
    """Run all test tiers and print summary."""
    print("\n")
    print("=" * 60)
    print("  UI Logic Tests — Mock Data (No API Quota Consumed)")
    print("=" * 60)

    total_passed = 0
    total_failed = 0

    for name, tests in [
        ("Main Tests (pure logic)", MAIN_TESTS),
        ("State Tests (mocked session_state)", STATE_TESTS),
        ("Render Tests (mocked st.markdown)", RENDER_TESTS),
    ]:
        p, f = run_tier(name, tests)
        total_passed += p
        total_failed += f

    print("\n")
    print("=" * 60)
    total = total_passed + total_failed
    print(f"  TOTAL: {total_passed}/{total} passed, {total_failed} failed")
    if total_failed == 0:
        print("  ALL TESTS PASSED")
    else:
        print(f"  {total_failed} TEST(S) FAILED")
    print("=" * 60 + "\n")

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
