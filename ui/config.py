"""Configuration for the Streamlit Web UI.

Manages API base URL, timeouts, and feature flag display names.
"""

import os
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class UIConfig:
    """Central configuration for the Web UI."""

    api_base_url: str = field(default_factory=lambda: os.getenv("UI_API_BASE_URL", "http://localhost:8000"))
    stream_timeout: float = 300.0
    request_timeout: float = 30.0
    approval_timeout: float = 60.0
    max_images: int = 5
    poll_interval_seconds: float = 2.0
    max_poll_retries: int = 3

    feature_flags: Dict[str, str] = field(
        default_factory=lambda: {
            "ENABLE_SMART_CODER": "Smart Coder",
            "ENABLE_MULTIMODAL": "Multimodal",
            "ENABLE_SCRIPT_EXECUTION": "Script Execution",
            "ENABLE_RESOURCE_QUOTA": "Resource Quota",
            "ENABLE_SMART_SEARCH": "Smart Search",
            "ENABLE_KNOWLEDGE_GRAPH": "Knowledge Graph",
            "ENABLE_SMART_EDITING": "Smart Editing",
            "ENABLE_CODE_INDEX": "Code Index",
        }
    )

    @property
    def auth_login_url(self) -> str:
        """Return the full URL for the login endpoint."""
        return f"{self.api_base_url}/api/v1/auth/login"

    @property
    def auth_register_url(self) -> str:
        """Return the full URL for the user registration endpoint."""
        return f"{self.api_base_url}/api/v1/auth/register"

    @property
    def auth_session_url(self) -> str:
        """Return the full URL for session creation endpoint."""
        return f"{self.api_base_url}/api/v1/auth/session"

    @property
    def sdlc_stream_url(self) -> str:
        """Return the full URL for the SDLC streaming endpoint."""
        return f"{self.api_base_url}/api/v1/sdlc/stream"

    @property
    def sdlc_approve_url(self) -> str:
        """Return the URL template (with {thread_id}) for approving a task."""
        return f"{self.api_base_url}/api/v1/sdlc/{{thread_id}}/approve"

    @property
    def sdlc_reject_url(self) -> str:
        """Return the URL template (with {thread_id}) for rejecting a task."""
        return f"{self.api_base_url}/api/v1/sdlc/{{thread_id}}/reject"

    @property
    def sdlc_tasks_url(self) -> str:
        """Return the full URL for listing SDLC tasks."""
        return f"{self.api_base_url}/api/v1/sdlc/tasks"

    @property
    def sdlc_state_url(self) -> str:
        """Return the URL template (with {thread_id}) for fetching SDLC state."""
        return f"{self.api_base_url}/api/v1/sdlc/{{thread_id}}/state"

    @property
    def sdlc_artifacts_url(self) -> str:
        """Return the URL template (with {thread_id}) for listing preview artifacts."""
        return f"{self.api_base_url}/api/v1/sdlc/{{thread_id}}/artifacts"

    @property
    def sdlc_feature_flags_url(self) -> str:
        """Return the URL for fetching server feature flag values."""
        return f"{self.api_base_url}/api/v1/sdlc/feature-flags"

    @property
    def sdlc_task_detail_url(self) -> str:
        """Return the URL template (with {task_id}) for fetching task detail."""
        return f"{self.api_base_url}/api/v1/sdlc/tasks/{{task_id}}"

    @property
    def sdlc_task_cancel_url(self) -> str:
        """Return the URL template (with {task_id}) for cancelling a task."""
        return f"{self.api_base_url}/api/v1/sdlc/tasks/{{task_id}}/cancel"

    @property
    def sdlc_task_delete_url(self) -> str:
        """Return the URL template (with {task_id}) for deleting a task."""
        return f"{self.api_base_url}/api/v1/sdlc/tasks/{{task_id}}"

    @property
    def media_image_url(self) -> str:
        """Return the URL for text-to-image generation."""
        return f"{self.api_base_url}/api/v1/media/generate-image"

    @property
    def media_video_url(self) -> str:
        """Return the URL for text-to-video generation."""
        return f"{self.api_base_url}/api/v1/media/generate-video"

    @property
    def media_history_url(self) -> str:
        """Return the URL for listing media generation history (L-14)."""
        return f"{self.api_base_url}/api/v1/media/history"


config = UIConfig()
