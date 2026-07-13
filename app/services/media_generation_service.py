"""Media generation service — DashScope native API.

Decoupled from LLMRegistry (which is OpenAI-compatible only). Handles
image and video generation via DashScope's native async HTTP API.

- Image (wan2.6-t2i): synchronous API, returns image URL directly.
- Video (wan2.7-t2v): async API, submit task + poll for result.

Both share ``_request`` to avoid redundant HTTP plumbing.
"""

import asyncio
from typing import Any, Optional

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import logger

_DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/api/v1"
# Video generation can take 1-5 minutes; allow generous polling.
_VIDEO_POLL_TIMEOUT = 300
_VIDEO_POLL_INTERVAL = 5


class MediaGenerationService:
    """DashScope native API service for image and video generation."""

    def __init__(self) -> None:
        """Initialize DashScope API credentials and headers."""
        self._api_key = settings.OPENAI_API_KEY
        self._base_url = _DASHSCOPE_BASE
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # L-7 fix: reuse a single ClientSession for connection pooling
        # instead of creating a new one per request. Lazily initialized
        # because aiohttp.ClientSession must be created inside a running
        # event loop.
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the shared aiohttp ClientSession."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=120),
            )
        return self._session

    async def close(self) -> None:
        """Close the shared session. Call on application shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def _request(
        self,
        path: str,
        body: dict[str, Any],
        async_mode: bool = False,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """Shared HTTP POST to DashScope native API.

        Args:
            path: API path under /api/v1.
            body: JSON request body.
            async_mode: If True, set X-DashScope-Async header (for video).
            timeout: Request timeout in seconds.

        Returns:
            Parsed JSON response dict.

        Raises:
            RuntimeError: On non-200 status from the API.
        """
        headers = dict(self._headers)
        if async_mode:
            headers["X-DashScope-Async"] = "enable"
        url = f"{self._base_url}{path}"
        session = await self._get_session()
        async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            data: dict[str, Any] = await resp.json()
            if resp.status != 200:
                error_msg = data.get("message", str(data))[:500]
                logger.error(
                    "dashscope_api_error",
                    path=path,
                    status=resp.status,
                    error=error_msg,
                )
                raise RuntimeError(f"DashScope API error {resp.status}: {error_msg}")
            return data

    async def _poll_task(self, task_id: str, timeout: int = _VIDEO_POLL_TIMEOUT) -> dict[str, Any]:
        """Poll an async task until SUCCEEDED or FAILED.

        Args:
            task_id: DashScope task ID returned by async submit.
            timeout: Max seconds to wait before giving up.

        Returns:
            Final task response dict (with video_url on success).

        Raises:
            RuntimeError: If task status is FAILED.
            TimeoutError: If task doesn't complete within timeout.
        """
        url = f"{self._base_url}/tasks/{task_id}"
        # L-8 fix: use get_running_loop() instead of the deprecated
        # get_event_loop() — we are always inside an async context here.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        session = await self._get_session()
        while loop.time() < deadline:
            async with session.get(url, headers=self._headers) as resp:
                data: dict[str, Any] = await resp.json()
            output = data.get("output", {})
            status = output.get("task_status", "")
            if status == "SUCCEEDED":
                return data
            if status == "FAILED":
                raise RuntimeError(f"Task {task_id} failed: {output}")
            logger.info("dashscope_task_polling", task_id=task_id, status=status)
            await asyncio.sleep(_VIDEO_POLL_INTERVAL)
        # M-11 fix: include task_id in the timeout error so the caller
        # can retry polling later without losing the task reference.
        raise TimeoutError(
            f"Task {task_id} timed out after {timeout}s. "
            f"The DashScope task is still running — retry with task_id={task_id} "
            f"via GET {_DASHSCOPE_BASE}/tasks/{task_id}"
        )

    async def generate_image(self, prompt: str, size: str = "1024*1024", n: int = 1) -> dict[str, Any]:
        """Text-to-image via wan2.6-t2i (synchronous API).

        Args:
            prompt: Image description (max ~2100 chars per Bailian spec).
            size: Image size, e.g. "1024*1024".
            n: Number of images to generate (1-4).

        Returns:
            Dict with ``images`` (list of URLs) and ``model``.

        Raises:
            RuntimeError: If the API returns no images (M-10 fix).
        """
        if not prompt.strip():
            raise ValueError("prompt must not be empty")

        body: dict[str, Any] = {
            "model": settings.IMAGE_GENERATION_MODEL,
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
            "parameters": {"size": size, "n": n},
        }
        logger.info(
            "media_image_generation_started",
            prompt_len=len(prompt),
            model=settings.IMAGE_GENERATION_MODEL,
            size=size,
        )
        result = await self._request("/services/aigc/multimodal-generation/generation", body, timeout=120)
        # Extract image URL(s) from the choices array.
        choices = result.get("output", {}).get("choices", [])
        images: list[str] = []
        for choice in choices:
            for item in choice.get("message", {}).get("content", []):
                if item.get("image"):
                    images.append(item["image"])
        # M-10 fix: fail loudly when the API returns no images instead
        # of silently returning an empty list that looks like success.
        if not images:
            raise RuntimeError(
                f"DashScope returned no images for prompt (len={len(prompt)}). Response: {str(result)[:500]}"
            )
        # M-14 fix: warn that DashScope image URLs expire in 24-48h.
        # Callers should persist the images locally if long-term access
        # is needed (e.g. via image_service.save_image_from_url).
        logger.warning(
            "media_image_url_temporary",
            image_count=len(images),
            notice="DashScope image URLs expire in 24-48h; persist locally for long-term access",
        )
        logger.info(
            "media_image_generation_completed",
            image_count=len(images),
            model=settings.IMAGE_GENERATION_MODEL,
        )
        return {"images": images, "model": settings.IMAGE_GENERATION_MODEL}

    async def generate_video(self, prompt: str, duration: int = 5, resolution: str = "720P") -> dict[str, Any]:
        """Text-to-video via wan2.7-t2v (async submit + poll).

        Args:
            prompt: Video description.
            duration: Video length in seconds (2-15).
            resolution: "720P" or "1080P".

        Returns:
            Dict with ``video_url``, ``task_id``, and ``model``.

        Raises:
            RuntimeError: If the API returns no video_url (M-10 fix).
            TimeoutError: If polling times out (includes task_id, M-11 fix).
        """
        if not prompt.strip():
            raise ValueError("prompt must not be empty")

        body: dict[str, Any] = {
            "model": settings.VIDEO_GENERATION_MODEL,
            "input": {"prompt": prompt},
            "parameters": {"resolution": resolution, "duration": duration},
        }
        logger.info(
            "media_video_generation_started",
            prompt_len=len(prompt),
            model=settings.VIDEO_GENERATION_MODEL,
            duration=duration,
            resolution=resolution,
        )
        # Step 1: submit async task.
        submit_result = await self._request(
            "/services/aigc/video-generation/generation",
            body,
            async_mode=True,
            timeout=30,
        )
        task_id = submit_result.get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"No task_id in submit response: {submit_result}")
        logger.info("media_video_task_submitted", task_id=task_id)

        # Step 2: poll for result (video generation takes 1-5 minutes).
        result = await self._poll_task(task_id)
        video_url = result.get("output", {}).get("video_url", "")
        # M-10 fix: fail loudly when the API succeeds but returns no URL.
        if not video_url:
            raise RuntimeError(
                f"DashScope task {task_id} succeeded but returned no video_url. Response: {str(result)[:500]}"
            )
        logger.info(
            "media_video_generation_completed",
            task_id=task_id,
            has_url=True,
        )
        return {
            "video_url": video_url,
            "task_id": task_id,
            "model": settings.VIDEO_GENERATION_MODEL,
        }


# Module-level singleton — matches the pattern used by other services
# (e.g. approval_log_service, sdlc_task_service).
media_service = MediaGenerationService()
