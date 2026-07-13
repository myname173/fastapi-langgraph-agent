"""Unified API client for the Streamlit Web UI.

Encapsulates all backend interactions:
- Authentication (login, register, session)
- SDLC task submission (streaming + non-streaming)
- Task approval/rejection
- Task listing and detail polling
- Server health check

Features:
- Centralized error handling with structured exceptions
- Automatic re-authentication on 401
- SSE stream parsing for LangGraph v2 events
- Configurable timeouts via UIConfig
"""

import json
from typing import Any, Dict, Iterator, List, Optional

import httpx

from ui.config import config


class APIError(Exception):
    """Base exception for API errors."""

    def __init__(self, message: str, status_code: int = 0):
        """Initialize APIError with message and optional status code.

        Args:
            message: Human-readable error message
            status_code: HTTP status code (0 if not applicable)
        """
        super().__init__(message)
        self.status_code = status_code


class ConnectionError(APIError):
    """Raised when the API server is unreachable."""

    pass


class AuthError(APIError):
    """Raised when authentication fails."""

    pass


class SDLCAPIClient:
    """Unified client for SDLC Orchestrator API."""

    def __init__(self, base_url: str = ""):
        """Initialize the SDLC API client.

        Args:
            base_url: API base URL; falls back to UIConfig.api_base_url when empty
        """
        self.base_url = base_url or config.api_base_url
        self._access_token: Optional[str] = None
        self._session_token: Optional[str] = None
        self._session_id: Optional[str] = None
        self._username: Optional[str] = None
        # Shared HTTP client — reuses TCP connections (keep-alive) across requests.
        # Default timeout is the request timeout; per-call `timeout=` overrides it.
        self._client: httpx.Client = httpx.Client(timeout=config.request_timeout)

    def close(self) -> None:
        """Close the underlying HTTP connection pool. Call on logout/session end."""
        self._client.close()

    @property
    def is_authenticated(self) -> bool:
        """Return True when the client holds an access or session token."""
        return self._session_token is not None or self._access_token is not None

    @property
    def username(self) -> Optional[str]:
        """Return the currently authenticated username, if any."""
        return self._username

    @property
    def session_id(self) -> Optional[str]:
        """Return the current session ID, if any."""
        return self._session_id

    def _headers(self) -> Dict[str, str]:
        token = self._session_token or self._access_token
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def _handle_http_error(self, resp: httpx.Response, action: str) -> None:
        """Raise appropriate exception for HTTP error responses."""
        if resp.status_code == 401:
            raise AuthError(f"Authentication failed for {action}", 401)
        elif resp.status_code == 404:
            raise APIError(f"Not found: {action}", 404)
        else:
            raise APIError(f"{action} failed: {resp.text}", resp.status_code)

    def _handle_connection_error(self, e: Exception) -> None:
        """Convert connection errors to user-friendly messages."""
        raise ConnectionError(f"Cannot connect to API at {self.base_url}. Is the server running?") from e

    def login(self, email: str, password: str) -> bool:
        """Login and create a session.

        Args:
            email: User email
            password: Password

        Returns:
            True if login succeeded

        Raises:
            APIError: On API failure
            ConnectionError: If server is unreachable
        """
        try:
            # Backend declares email/password/grant_type as Form(...) fields,
            # so we MUST send form-urlencoded data (not JSON) or get 422.
            resp = self._client.post(
                config.auth_login_url,
                data={
                    "email": email,
                    "password": password,
                    "grant_type": "password",
                },
                timeout=config.request_timeout,
            )
            if resp.status_code != 200:
                raise APIError(f"Login failed: {resp.text}", resp.status_code)

            self._access_token = resp.json()["access_token"]
            self._username = email
            return self._create_session()

        except httpx.ConnectError as e:
            self._handle_connection_error(e)
            return False

    def register(self, email: str, username: str, password: str) -> bool:
        """Register a new user and auto-login.

        Args:
            email: User email
            username: Username
            password: Password

        Returns:
            True if registration succeeded
        """
        try:
            resp = self._client.post(
                config.auth_register_url,
                json={"email": email, "username": username, "password": password},
                timeout=config.request_timeout,
            )
            if resp.status_code != 200:
                raise APIError(f"Registration failed: {resp.text}", resp.status_code)

            # Backend returns UserResponse.token as a Token object
            # ({access_token, token_type, expires_at}), not a bare string.
            self._access_token = resp.json()["token"]["access_token"]
            self._username = username
            return self._create_session()

        except httpx.ConnectError as e:
            self._handle_connection_error(e)
            return False

    def _create_session(self) -> bool:
        """Create a session using the access token."""
        if not self._access_token:
            return False

        try:
            resp = self._client.post(
                config.auth_session_url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                timeout=config.request_timeout,
            )
            if resp.status_code != 200:
                raise APIError(f"Session creation failed: {resp.text}", resp.status_code)

            data = resp.json()
            # Backend returns SessionResponse.token as a Token object
            # ({access_token, token_type, expires_at}), not a bare string.
            self._session_token = data["token"]["access_token"]
            self._session_id = data["session_id"]
            return True
        except httpx.ConnectError as e:
            self._handle_connection_error(e)
            return False

    def logout(self) -> None:
        """Clear all tokens and session data. Does not close the HTTP client."""
        self._access_token = None
        self._session_token = None
        self._session_id = None
        self._username = None

    def restore_auth(
        self,
        access_token: Optional[str],
        session_token: Optional[str],
        session_id: Optional[str],
        username: Optional[str],
    ) -> None:
        """Restore authentication tokens from persisted session state.

        Streamlit session_state can lose the SDLCAPIClient instance's
        attributes on certain reruns; the UI persists tokens in
        separate session_state fields and calls this to restore them.

        Args:
            access_token: JWT access token (user-scoped)
            session_token: JWT session token (session-scoped, used for SDLC)
            session_id: Session ID (also used as thread_id for SDLC)
            username: Display name (email or username)
        """
        self._access_token = access_token
        self._session_token = session_token
        self._session_id = session_id
        self._username = username

    def check_server_health(self) -> bool:
        """Check if the API server is reachable.

        Returns:
            True if server responds, False otherwise
        """
        try:
            resp = self._client.get(f"{self.base_url}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def stream_sdlc_task(
        self,
        requirements: str,
        project_context: str = "",
        images: Optional[List[str]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Submit a streaming SDLC task and yield parsed SSE events.

        Args:
            requirements: Task requirements
            project_context: Optional project context
            images: Optional list of base64-encoded images

        Yields:
            Parsed event dictionaries from SSE stream

        Raises:
            ConnectionError: If server is unreachable
            AuthError: If authentication is invalid
        """
        payload: Dict[str, Any] = {"requirements": requirements}
        if project_context:
            payload["project_context"] = project_context
        if images:
            payload["images"] = images

        try:
            with self._client.stream(
                "POST",
                config.sdlc_stream_url,
                json=payload,
                headers=self._headers(),
                timeout=config.stream_timeout,
            ) as response:
                if response.status_code == 401:
                    raise AuthError("Authentication expired. Please login again.", 401)
                if response.status_code != 200:
                    raise APIError(
                        f"Stream request failed: {response.text}",
                        response.status_code,
                    )

                for line in response.iter_lines():
                    if line.startswith("data: "):
                        json_str = line[6:]
                        try:
                            yield json.loads(json_str)
                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectError as e:
            self._handle_connection_error(e)

    def approve_task(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Approve a task requiring human approval.

        Args:
            thread_id: Thread ID of the task

        Returns:
            Response dict on success, None on failure
        """
        try:
            resp = self._client.post(
                config.sdlc_approve_url.format(thread_id=thread_id),
                headers=self._headers(),
                timeout=config.approval_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            self._handle_http_error(resp, "approve task")
            return None
        except httpx.ConnectError as e:
            self._handle_connection_error(e)
            return None

    def reject_task(self, thread_id: str, reason: str = "Rejected by user") -> Optional[Dict[str, Any]]:
        """Reject a task requiring human approval.

        Args:
            thread_id: Thread ID of the task
            reason: Rejection reason

        Returns:
            Response dict on success, None on failure
        """
        try:
            # Backend declares `reason: str = Body(default="", embed=True)`,
            # so the JSON body must be {"reason": "..."} — not a bare string.
            resp = self._client.post(
                config.sdlc_reject_url.format(thread_id=thread_id),
                json={"reason": reason},
                headers={**self._headers(), "Content-Type": "application/json"},
                timeout=config.approval_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            self._handle_http_error(resp, "reject task")
            return None
        except httpx.ConnectError as e:
            self._handle_connection_error(e)
            return None

    def fetch_tasks(self) -> List[Dict[str, Any]]:
        """Fetch task list for the current user.

        Returns:
            List of task dicts (empty on error)
        """
        try:
            resp = self._client.get(
                config.sdlc_tasks_url,
                headers=self._headers(),
                timeout=config.request_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception:
            return []

    def fetch_task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch details for a specific task.

        Args:
            task_id: Task ID

        Returns:
            Task dict on success, None on failure
        """
        try:
            resp = self._client.get(
                config.sdlc_task_detail_url.format(task_id=task_id),
                headers=self._headers(),
                timeout=config.request_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    def fetch_state(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the full SDLC state for a thread.

        Called by the UI after the SSE stream ends to render the final
        code_changes / plan / messages / is_complete — astream_events
        on_chain_end events wrap node outputs in Command objects the UI
        should not have to parse.

        Args:
            thread_id: Thread ID (same as session_id used for streaming)

        Returns:
            Full SDLCState dict on success, None on failure
        """
        try:
            resp = self._client.get(
                config.sdlc_state_url.format(thread_id=thread_id),
                headers=self._headers(),
                timeout=config.request_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    def get_artifacts(self, thread_id: str) -> List[Dict[str, Any]]:
        """Fetch previewable artifacts for a thread.

        Args:
            thread_id: Thread ID (same as session_id used for streaming)

        Returns:
            List of artifact dicts (empty on error or when no artifacts
            have been registered for this thread).
        """
        try:
            resp = self._client.get(
                config.sdlc_artifacts_url.format(thread_id=thread_id),
                headers=self._headers(),
                timeout=config.request_timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("artifacts", []) if isinstance(data, dict) else []
            return []
        except Exception:
            return []

    def delete_task(self, task_id: str) -> bool:
        """Delete an SDLC task.

        Args:
            task_id: Task ID to delete.

        Returns:
            True on success, False on failure.
        """
        try:
            resp = self._client.delete(
                config.sdlc_task_delete_url.format(task_id=task_id),
                headers=self._headers(),
                timeout=config.request_timeout,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def generate_image(self, prompt: str, size: str = "1024*1024", n: int = 1) -> Optional[Dict[str, Any]]:
        """Generate image(s) via the media API endpoint.

        Args:
            prompt: Image description.
            size: Image size, e.g. "1024*1024".
            n: Number of images (1-4).

        Returns:
            Dict with ``images`` (list of URLs) and ``model``, or None on failure.
        """
        try:
            resp = self._client.post(
                config.media_image_url,
                json={"prompt": prompt, "size": size, "n": n},
                headers=self._headers(),
                timeout=120,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    def generate_video(self, prompt: str, duration: int = 5, resolution: str = "720P") -> Optional[Dict[str, Any]]:
        """Generate a video via the media API endpoint (async, may take minutes).

        Args:
            prompt: Video description.
            duration: Video length in seconds (2-15).
            resolution: "720P" or "1080P".

        Returns:
            Dict with ``video_url``, ``task_id``, and ``model``, or None on failure.
        """
        try:
            resp = self._client.post(
                config.media_video_url,
                json={"prompt": prompt, "duration": duration, "resolution": resolution},
                headers=self._headers(),
                timeout=300,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    def fetch_media_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch the current user's media generation history (L-14).

        Args:
            limit: Maximum number of records to return (1-200).

        Returns:
            List of history dicts (newest first), empty on error.
        """
        try:
            resp = self._client.get(
                config.media_history_url,
                params={"limit": limit},
                headers=self._headers(),
                timeout=config.request_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception:
            return []

    def download_bytes(self, url: str) -> bytes:
        """Download raw bytes from a URL (e.g. generated image URL).

        Used by ``st.download_button`` to let users save generated assets.
        No auth header — these are public DashScope OSS URLs.

        Args:
            url: The asset URL returned by the media API.

        Returns:
            Raw bytes of the asset, or empty bytes on failure.
        """
        try:
            resp = self._client.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.content
            return b""
        except Exception:
            return b""

    def fetch_feature_flags(self) -> Dict[str, bool]:
        """Fetch the actual feature flag values from the server.

        ISSUE-9 fix: the sidebar previously only showed flag names.
        Now it can display enabled/disabled status from the backend.

        Returns:
            Dict mapping flag name to its boolean value, or empty dict
            on failure.
        """
        try:
            resp = self._client.get(
                config.sdlc_feature_flags_url,
                headers=self._headers(),
                timeout=config.request_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return {}
        except Exception:
            return {}
