"""UI preview logic tests — mock data, no API quota consumed.

Tests the Preview tab rendering logic (module D) and the get_artifacts
API client method. All Streamlit functions are mocked; no real rendering
or HTTP calls occur.

Test tiers:
1. Main: pure logic (URL construction, API method with mocked httpx)
2. State: render_preview_tab with mocked session_state (no client / no thread / empty)
3. Render: render_preview_tab with mocked st.* (html/image/multi artifact dispatch)

Run inside the UI container:
    docker compose exec ui /app/.venv/bin/python ui/tests/test_preview_logic.py
Or with pytest:
    docker compose exec ui /app/.venv/bin/python -m pytest ui/tests/test_preview_logic.py -v
"""

from typing import Dict, List
from unittest.mock import patch, MagicMock

# ============================================================================
# Mock Data Fixtures
# ============================================================================

MOCK_ARTIFACT_HTML: Dict = {
    "name": "Snake Game",
    "path": "index.html",
    "type": "html",
    "description": "A web-based snake game with AI-generated assets",
    "preview_url": "/workspace/test-thread/index.html",
    "absolute_url": "http://localhost:8000/workspace/test-thread/index.html",
    "registered_at": "2026-07-10T12:00:00+00:00",
}

MOCK_ARTIFACT_IMAGE: Dict = {
    "name": "Game Background",
    "path": "assets/images/bg.png",
    "type": "image",
    "description": "AI-generated background image",
    "preview_url": "/workspace/test-thread/assets/images/bg.png",
    "absolute_url": "http://localhost:8000/workspace/test-thread/assets/images/bg.png",
    "registered_at": "2026-07-10T12:01:00+00:00",
}

MOCK_ARTIFACT_VIDEO: Dict = {
    "name": "Intro Video",
    "path": "assets/intro.mp4",
    "type": "video",
    "description": "Generated intro animation",
    "preview_url": "/workspace/test-thread/assets/intro.mp4",
    "absolute_url": "http://localhost:8000/workspace/test-thread/assets/intro.mp4",
    "registered_at": "2026-07-10T12:02:00+00:00",
}

MOCK_ARTIFACTS_MULTI: List[Dict] = [MOCK_ARTIFACT_HTML, MOCK_ARTIFACT_IMAGE, MOCK_ARTIFACT_VIDEO]

MOCK_ARTIFACTS_EMPTY: List[Dict] = []


# ============================================================================
# Tier 1: Main Tests (pure logic, no Streamlit)
# ============================================================================


def test_artifacts_url_template():
    """Test sdlc_artifacts_url property contains {thread_id} placeholder."""
    from ui.config import config

    template = config.sdlc_artifacts_url
    assert "{thread_id}" in template
    assert template.endswith("/artifacts")

    formatted = template.format(thread_id="abc-123")
    assert "abc-123" in formatted
    assert formatted.endswith("/artifacts")
    print("  PASS: artifacts_url_template")


def test_url_construction():
    """Test full URL = api_base_url + preview_url (used in preview rendering)."""
    from ui.config import config

    preview_url = "/workspace/test-thread/index.html"
    full_url = f"{config.api_base_url}{preview_url}"

    assert full_url.startswith("http")
    assert "workspace/test-thread/index.html" in full_url
    # Should not contain double slashes (except http://)
    assert "//workspace" not in full_url
    print("  PASS: url_construction")


def test_get_artifacts_api_success():
    """Test get_artifacts parses backend response correctly."""
    from ui.api_client import SDLCAPIClient

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"thread_id": "test", "artifacts": MOCK_ARTIFACTS_MULTI}

    mock_http = MagicMock()
    mock_http.get.return_value = mock_resp

    client = SDLCAPIClient()
    client._client = mock_http

    result = client.get_artifacts("test-thread")

    assert len(result) == 3
    assert result[0]["name"] == "Snake Game"
    assert result[1]["type"] == "image"
    assert result[2]["name"] == "Intro Video"

    # Verify URL was constructed with thread_id
    mock_http.get.assert_called_once()
    call_url = mock_http.get.call_args[0][0]
    assert "test-thread" in call_url
    assert "artifacts" in call_url
    print("  PASS: get_artifacts_api_success")


def test_get_artifacts_api_empty():
    """Test get_artifacts returns empty list when backend has no artifacts."""
    from ui.api_client import SDLCAPIClient

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"thread_id": "test", "artifacts": []}

    mock_http = MagicMock()
    mock_http.get.return_value = mock_resp

    client = SDLCAPIClient()
    client._client = mock_http

    result = client.get_artifacts("empty-thread")
    assert result == []
    print("  PASS: get_artifacts_api_empty")


def test_get_artifacts_api_error():
    """Test get_artifacts returns empty list on HTTP error (no exception raised)."""
    from ui.api_client import SDLCAPIClient

    mock_resp = MagicMock()
    mock_resp.status_code = 500

    mock_http = MagicMock()
    mock_http.get.return_value = mock_resp

    client = SDLCAPIClient()
    client._client = mock_http

    result = client.get_artifacts("error-thread")
    assert result == []
    print("  PASS: get_artifacts_api_error")


def test_get_artifacts_api_connection_error():
    """Test get_artifacts returns empty list on connection error."""
    from ui.api_client import SDLCAPIClient
    import httpx

    mock_http = MagicMock()
    mock_http.get.side_effect = httpx.ConnectError("Connection refused")

    client = SDLCAPIClient()
    client._client = mock_http

    result = client.get_artifacts("conn-error")
    assert result == []
    print("  PASS: get_artifacts_api_connection_error")


# ============================================================================
# Tier 2: State Tests (mocked session_state)
# ============================================================================


def test_render_no_client():
    """Test render_preview_tab shows prompt when no API client exists."""
    from ui.components.preview import render_preview_tab

    mock_state: Dict = {}  # No api_client
    mock_caption = MagicMock()

    with patch("streamlit.session_state", mock_state), patch("streamlit.caption", mock_caption):
        render_preview_tab()

    mock_caption.assert_called()
    print("  PASS: render_no_client")


def test_render_no_thread_id():
    """Test render_preview_tab shows prompt when no thread_id available."""
    from ui.components.preview import render_preview_tab

    mock_client = MagicMock()
    mock_client.session_id = None
    mock_state: Dict = {"api_client": mock_client}  # No current_thread_id
    mock_caption = MagicMock()

    with patch("streamlit.session_state", mock_state), patch("streamlit.caption", mock_caption):
        render_preview_tab()

    mock_caption.assert_called()
    print("  PASS: render_no_thread_id")


def test_render_empty_artifacts():
    """Test render_preview_tab shows 'no artifacts' message when list is empty."""
    from ui.components.preview import render_preview_tab

    mock_client = MagicMock()
    mock_client.session_id = "test-thread"
    mock_client.get_artifacts.return_value = MOCK_ARTIFACTS_EMPTY
    mock_state: Dict = {"api_client": mock_client, "current_thread_id": "test-thread"}
    mock_caption = MagicMock()

    with patch("streamlit.session_state", mock_state), patch("streamlit.caption", mock_caption):
        render_preview_tab()

    mock_caption.assert_called()
    caption_text = mock_caption.call_args[0][0]
    assert "No previewable" in caption_text or "no" in caption_text.lower()
    print("  PASS: render_empty_artifacts")


# ============================================================================
# Tier 3: Render Tests (mocked st.* functions — secondary)
# ============================================================================


def test_render_html_artifact():
    """Test HTML artifact renders via sandboxed iframe (components.html)."""
    from ui.components.preview import render_preview_tab

    mock_client = MagicMock()
    mock_client.session_id = "test-thread"
    mock_client.get_artifacts.return_value = [MOCK_ARTIFACT_HTML]
    mock_state: Dict = {"api_client": mock_client, "current_thread_id": "test-thread"}

    mock_md = MagicMock()
    mock_caption = MagicMock()
    mock_html = MagicMock()

    with (
        patch("streamlit.session_state", mock_state),
        patch("streamlit.markdown", mock_md),
        patch("streamlit.caption", mock_caption),
        patch("streamlit.components.v1.html", mock_html),
    ):
        render_preview_tab()

    # components.html should be called with an iframe containing sandbox="allow-scripts"
    mock_html.assert_called_once()
    html_arg = mock_html.call_args[0][0]
    assert "iframe" in html_arg
    assert "sandbox" in html_arg
    assert "allow-scripts" in html_arg
    # Must NOT contain allow-same-origin (security: sandbox escape risk)
    assert "allow-same-origin" not in html_arg
    # URL should contain the preview_url path
    assert "index.html" in html_arg
    print("  PASS: render_html_artifact (sandbox=allow-scripts, no same-origin)")


def test_render_image_artifact():
    """Test image artifact renders via st.image with correct URL."""
    from ui.components.preview import render_preview_tab

    mock_client = MagicMock()
    mock_client.session_id = "test-thread"
    mock_client.get_artifacts.return_value = [MOCK_ARTIFACT_IMAGE]
    mock_state: Dict = {"api_client": mock_client, "current_thread_id": "test-thread"}

    mock_md = MagicMock()
    mock_caption = MagicMock()
    mock_image = MagicMock()

    with (
        patch("streamlit.session_state", mock_state),
        patch("streamlit.markdown", mock_md),
        patch("streamlit.caption", mock_caption),
        patch("streamlit.image", mock_image),
    ):
        render_preview_tab()

    mock_image.assert_called_once()
    url_arg = mock_image.call_args[0][0]
    # URL should be full URL (api_base_url + preview_url)
    assert "bg.png" in url_arg
    assert url_arg.startswith("http")
    print("  PASS: render_image_artifact")


def test_render_video_artifact():
    """Test video artifact renders via st.video with correct URL."""
    from ui.components.preview import render_preview_tab

    mock_client = MagicMock()
    mock_client.session_id = "test-thread"
    mock_client.get_artifacts.return_value = [MOCK_ARTIFACT_VIDEO]
    mock_state: Dict = {"api_client": mock_client, "current_thread_id": "test-thread"}

    mock_md = MagicMock()
    mock_caption = MagicMock()
    mock_video = MagicMock()

    with (
        patch("streamlit.session_state", mock_state),
        patch("streamlit.markdown", mock_md),
        patch("streamlit.caption", mock_caption),
        patch("streamlit.video", mock_video),
    ):
        render_preview_tab()

    mock_video.assert_called_once()
    url_arg = mock_video.call_args[0][0]
    assert "intro.mp4" in url_arg
    assert url_arg.startswith("http")
    print("  PASS: render_video_artifact")


def test_render_multiple_artifacts_selector():
    """Test multiple artifacts show a selectbox for choosing."""
    from ui.components.preview import render_preview_tab

    mock_client = MagicMock()
    mock_client.session_id = "test-thread"
    mock_client.get_artifacts.return_value = MOCK_ARTIFACTS_MULTI
    mock_state: Dict = {"api_client": mock_client, "current_thread_id": "test-thread"}

    mock_md = MagicMock()
    mock_caption = MagicMock()
    mock_selectbox = MagicMock(return_value=0)  # Select first artifact (HTML)

    with (
        patch("streamlit.session_state", mock_state),
        patch("streamlit.markdown", mock_md),
        patch("streamlit.caption", mock_caption),
        patch("streamlit.selectbox", mock_selectbox),
        patch("streamlit.components.v1.html", MagicMock()),
    ):
        render_preview_tab()

    # selectbox should be called when multiple artifacts exist
    mock_selectbox.assert_called_once()
    print("  PASS: render_multiple_artifacts_selector")


# ============================================================================
# Test Runner
# ============================================================================

MAIN_TESTS = [
    test_artifacts_url_template,
    test_url_construction,
    test_get_artifacts_api_success,
    test_get_artifacts_api_empty,
    test_get_artifacts_api_error,
    test_get_artifacts_api_connection_error,
]

STATE_TESTS = [
    test_render_no_client,
    test_render_no_thread_id,
    test_render_empty_artifacts,
]

RENDER_TESTS = [
    test_render_html_artifact,
    test_render_image_artifact,
    test_render_video_artifact,
    test_render_multiple_artifacts_selector,
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
    print("  Preview Logic Tests — Mock Data (No API Quota Consumed)")
    print("=" * 60)

    total_passed = 0
    total_failed = 0

    for name, tests in [
        ("Main Tests (pure logic + API mock)", MAIN_TESTS),
        ("State Tests (mocked session_state)", STATE_TESTS),
        ("Render Tests (mocked st.* functions)", RENDER_TESTS),
    ]:
        p, f = run_tier(name, tests)
        total_passed += p
        total_failed += f

    print(f"\n{'=' * 60}")
    print(f"  Total: {total_passed} passed, {total_failed} failed")
    print(f"{'=' * 60}")

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
