"""Image processing service for multimodal support.

This module provides:
- Image upload and storage management
- Vision LLM integration for image analysis
- Cleanup mechanisms for temporary image files
"""

import base64
import hashlib
import io
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from PIL import Image
except ImportError:
    Image = None

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.service import llm_service
from app.services.quota_service import quota_service
from app.schemas import ImageAnalysisResult


class ImageService:
    """Service for handling image uploads, analysis, and cleanup."""

    def __init__(self):
        """Initialize the image service with configured limits."""
        self._max_size_bytes = settings.MAX_IMAGE_SIZE_MB * 1024 * 1024
        self._allowed_types = {t.lower() for t in settings.ALLOWED_IMAGE_TYPES}
        self._analysis_cache: Dict[str, Dict[str, Any]] = {}

    def _clean_analysis_cache(self) -> None:
        """Remove expired entries from the analysis cache."""
        now = time.time()
        ttl = settings.IMAGE_ANALYSIS_CACHE_TTL_SECONDS
        expired = [key for key, entry in self._analysis_cache.items() if now - entry["timestamp"] > ttl]
        for key in expired:
            del self._analysis_cache[key]

    def _get_cache_key(self, image_id: str, context_query: str) -> str:
        """Generate a cache key for image analysis.

        Args:
            image_id: Image identifier
            context_query: Context query string

        Returns:
            Cache key string
        """
        return f"{image_id}:{hashlib.sha256(context_query.encode()).hexdigest()[:8]}"

    def _get_image_dir(self, task_id: str) -> Path:
        """Get the directory for storing images for a specific task.

        Args:
            task_id: Task ID for isolation

        Returns:
            Path to the images directory
        """
        image_dir = settings.SDLC_WORKSPACE_ROOT / task_id / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        return image_dir

    def _validate_image(self, image_data: bytes) -> tuple[bool, Optional[str]]:
        """Validate image data for security and size constraints.

        Args:
            image_data: Raw image bytes

        Returns:
            Tuple of (is_valid, error_message)
        """
        if len(image_data) > self._max_size_bytes:
            return False, f"Image size exceeds {settings.MAX_IMAGE_SIZE_MB}MB limit"

        try:
            if Image is None:
                raise ImportError("PIL/Pillow is not installed")
            image = Image.open(io.BytesIO(image_data))
            image.verify()
            image_format = image.format.lower() if image.format else ""

            if image_format not in self._allowed_types:
                return False, f"Image format {image_format} not allowed. Allowed: {', '.join(self._allowed_types)}"

            return True, None
        except Exception as e:
            return False, f"Invalid image data: {str(e)}"

    def _generate_image_id(self, image_data: bytes, task_id: str) -> str:
        """Generate a unique image ID based on content hash.

        Args:
            image_data: Raw image bytes
            task_id: Task ID for namespace

        Returns:
            Unique image ID string
        """
        content_hash = hashlib.sha256(image_data).hexdigest()[:16]
        return f"{task_id[:8]}-{content_hash}"

    async def save_uploaded_image(self, image_data: bytes, task_id: str) -> str:
        """Save an uploaded image to the task's image directory.

        Args:
            image_data: Raw image bytes
            task_id: Task ID for isolation

        Returns:
            image_id for subsequent operations

        Raises:
            ValueError: If image validation fails or quota exceeded
        """
        if not settings.ENABLE_MULTIMODAL:
            raise ValueError("Multimodal support is disabled")

        if not await quota_service.check_image_count_quota_async(task_id):
            raise ValueError(f"Image count quota exceeded for task {task_id} (max: {settings.MAX_IMAGES_PER_TASK})")

        if not await quota_service.check_workspace_quota_async(task_id):
            raise ValueError(
                f"Workspace size quota exceeded for task {task_id} (max: {settings.MAX_WORKSPACE_SIZE_MB}MB)"
            )

        is_valid, error = self._validate_image(image_data)
        if not is_valid:
            raise ValueError(error)

        image_id = self._generate_image_id(image_data, task_id)
        image_dir = self._get_image_dir(task_id)

        if Image is None:
            raise ImportError("PIL/Pillow is not installed")
        image = Image.open(io.BytesIO(image_data))
        image_format = image.format.lower() if image.format else "png"
        image_path = image_dir / f"{image_id}.{image_format}"

        image.save(image_path)
        logger.info("image_saved", image_id=image_id, task_id=task_id, path=str(image_path))

        return image_id

    async def save_image_from_url(self, url: str, task_id: str) -> str:
        """Save an image from a URL to the task's image directory.

        Args:
            url: URL of the image
            task_id: Task ID for isolation

        Returns:
            image_id for subsequent operations

        Raises:
            ValueError: If image validation fails or download fails
        """
        import aiohttp

        if not settings.ENABLE_MULTIMODAL:
            raise ValueError("Multimodal support is disabled")

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise ValueError(f"Failed to download image: HTTP {response.status}")

                    image_data = await response.read()
                    return await self.save_uploaded_image(image_data, task_id)

        except Exception as e:
            logger.error("image_download_failed", url=url, error=str(e))
            raise ValueError(f"Failed to download image: {str(e)}")

    def get_image_path(self, image_id: str, task_id: str) -> Optional[Path]:
        """Get the path to a saved image.

        Args:
            image_id: Image ID
            task_id: Task ID

        Returns:
            Path to the image file or None if not found
        """
        image_dir = self._get_image_dir(task_id)

        for ext in self._allowed_types:
            candidate_path = image_dir / f"{image_id}.{ext}"
            if candidate_path.exists():
                return candidate_path

        return None

    async def analyze_image(
        self,
        image_id_or_url: str,
        task_id: str,
        context_query: str = "",
    ) -> ImageAnalysisResult:
        """Analyze an image using Vision LLM.

        Args:
            image_id_or_url: Either an image_id (from save_uploaded_image) or a URL
            task_id: Task ID for workspace isolation
            context_query: Optional context query to guide the analysis

        Returns:
            ImageAnalysisResult with description, key_elements, and suggestions
        """
        if not settings.ENABLE_MULTIMODAL:
            return ImageAnalysisResult(
                image_id=image_id_or_url,
                description="Multimodal support is disabled.",
                key_elements=[],
                suggestions=[],
            )

        self._clean_analysis_cache()

        image_path = self.get_image_path(image_id_or_url, task_id)

        if image_path and image_path.exists():
            with open(image_path, "rb") as f:
                image_data = f.read()
            image_id = image_id_or_url
        else:
            try:
                image_id = await self.save_image_from_url(image_id_or_url, task_id)
                image_path = self.get_image_path(image_id, task_id)
                if image_path is None:
                    raise ValueError("Image path not found after upload")
                with open(image_path, "rb") as f:
                    image_data = f.read()
            except Exception as e:
                logger.error("image_analysis_failed", image_id=image_id_or_url, error=str(e))
                return ImageAnalysisResult(
                    image_id=image_id_or_url,
                    description=f"Failed to analyze image: {str(e)}",
                    key_elements=[],
                    suggestions=[],
                )

        cache_key = self._get_cache_key(image_id, context_query)
        if cache_key in self._analysis_cache:
            logger.info("image_analysis_cache_hit", image_id=image_id, task_id=task_id)
            return ImageAnalysisResult(**self._analysis_cache[cache_key]["result"])

        try:
            base64_image = base64.b64encode(image_data).decode("utf-8")
            image_format = image_path.suffix[1:].lower() if image_path else "png"
            image_url = f"data:image/{image_format};base64,{base64_image}"

            prompt = f"""
                Analyze this image and provide a detailed description.
                {f"Context: {context_query}" if context_query else ""}

                Provide:
                1. A comprehensive description of what is shown in the image
                2. Key elements/objects identified in the image
                3. Suggestions or observations relevant to the context

                Return ONLY a valid JSON object with:
                - description: string
                - key_elements: array of strings
                - suggestions: array of strings
            """.strip()

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ]

            response = await llm_service.call(
                messages,
                model_name=settings.VISION_MODEL,
                response_format=ImageAnalysisResult,
                metadata={"task_id": task_id, "image_id": image_id},
            )

            response.image_id = image_id

            self._analysis_cache[cache_key] = {
                "result": response.model_dump(),
                "timestamp": time.time(),
            }

            logger.info("image_analysis_completed", image_id=image_id, task_id=task_id)
            return response

        except Exception as e:
            logger.error("vision_llm_call_failed", image_id=image_id, error=str(e))
            return ImageAnalysisResult(
                image_id=image_id,
                description=f"Image analysis unavailable: {str(e)}",
                key_elements=[],
                suggestions=[],
            )

    async def cleanup_task_images(self, task_id: str) -> int:
        """Clean up all images associated with a task.

        Args:
            task_id: Task ID

        Returns:
            Number of files deleted
        """
        image_dir = self._get_image_dir(task_id)
        deleted_count = 0

        try:
            for image_file in image_dir.glob("*.*"):
                if image_file.suffix[1:].lower() in self._allowed_types:
                    image_file.unlink()
                    deleted_count += 1

            if image_dir.exists() and not any(image_dir.iterdir()):
                image_dir.rmdir()

            logger.info("images_cleaned_up", task_id=task_id, deleted_count=deleted_count)
            return deleted_count

        except Exception as e:
            logger.error("image_cleanup_failed", task_id=task_id, error=str(e))
            return 0


image_service = ImageService()
