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
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from PIL import Image
except ImportError:
    Image = None

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.service import llm_service
from app.services.llm.registry import LLMRegistry
from app.services.quota_service import quota_service
from app.schemas import ImageAnalysisResult
from app.schemas.chat import InvestmentImageAnalysis


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

    _INVESTMENT_VISION_PROMPT = """你是一名严谨的投资/股票知识助手。用户上传了一张与投资相关的图片（如K线图、行情截图、财务报表、券商研报等）。

请：
1. 用通俗中文描述图片中可见的内容与关键要素（如标的名称、价格、涨跌幅、时间范围、图表类型、重要数字或标签）。
2. 只做客观描述与信息提取；不要给出买卖建议，不要预测涨跌，不要推荐具体标的。
3. 在描述末尾附加一句：以上仅为图片内容解读，不构成任何投资建议；投资有风险，决策需谨慎。

以结构化结果返回：description 为通俗描述，key_elements 为关键要素列表。"""

    async def analyze_investment_image(
        self,
        image_input: str,
        task_id: str = "chat",
        user_id: Optional[int] = None,
        temp: bool = False,
    ) -> Optional[InvestmentImageAnalysis]:
        """Analyze an investment-related image for the chat mode (Route B).

        Chat-only path: describe-only (no buy/sell advice). Accepts an http(s)
        URL or a base64 ``data:image/...;base64,...`` string. On any failure
        (multimodal disabled, decode error, VLM unreachable, circuit open)
        returns ``None`` so the caller degrades to plain-text chat instead of
        raising.

        Args:
            image_input: http(s) URL or base64 image data URL.
            task_id: Namespace for the persisted image. Pass the real task id
                for SDLC tasks; for transient chat uploads prefer ``temp=True``.
            user_id: Optional user id, used only for logging context.
            temp: When ``True`` the image is stored under an ephemeral
                per-request namespace and deleted immediately after analysis.
                This keeps chat uploads from accumulating in the session
                workspace or exhausting ``MAX_IMAGES_PER_TASK`` over a long
                conversation.

        Returns:
            InvestmentImageAnalysis or None on failure / disabled.
        """
        if not settings.ENABLE_MULTIMODAL:
            logger.info("vision_analysis_disabled", task_id=task_id)
            return None

        # Transient chat uploads use a throwaway namespace that is cleaned up
        # after analysis (see finally below). This prevents them from counting
        # against the session's MAX_IMAGES_PER_TASK quota or polluting the
        # persistent SDLC workspace.
        temp_task_id = f"chat-tmp-{uuid.uuid4().hex}" if temp else None
        if temp_task_id is not None:
            task_id = temp_task_id

        try:
            # 1) Resolve image bytes (base64 data URL or remote URL).
            if image_input.startswith("data:image"):
                header, _, payload = image_input.partition(",")
                if ";base64" not in header:
                    logger.warning("vision_unknown_data_url", task_id=task_id)
                    return None
                try:
                    image_bytes = base64.b64decode(payload)
                except Exception:
                    logger.warning("vision_base64_decode_failed", task_id=task_id)
                    return None
                image_id = await self.save_uploaded_image(image_bytes, task_id)
            elif image_input.startswith(("http://", "https://")):
                image_id = await self.save_image_from_url(image_input, task_id)
            else:
                logger.warning("vision_unsupported_input", task_id=task_id)
                return None

            # 2) Build the data URL the VLM consumes.
            image_path = self.get_image_path(image_id, task_id)
            if image_path is None or not image_path.exists():
                logger.warning("vision_image_path_missing", task_id=task_id, image_id=image_id)
                return None
            with open(image_path, "rb") as f:
                image_data = f.read()
            image_format = image_path.suffix[1:].lower()
            image_url = f"data:image/{image_format};base64,{base64.b64encode(image_data).decode('utf-8')}"

            # 3) Ensure the vision model is registered so LLMService can route it.
            LLMRegistry.ensure_model(settings.VISION_MODEL)

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._INVESTMENT_VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ]
            response = await llm_service.call(
                messages,
                model_name=settings.VISION_MODEL,
                response_format=InvestmentImageAnalysis,
                max_tokens=settings.VISION_MAX_TOKENS,
                metadata={"task_id": task_id, "image_id": image_id},
            )

            if isinstance(response, InvestmentImageAnalysis):
                return response
            # Defensive: coerce unexpected shapes into the expected schema.
            return InvestmentImageAnalysis(
                image_id=image_id,
                description=getattr(response, "description", str(response)),
                key_elements=getattr(response, "key_elements", []) or [],
            )
        except Exception as e:
            logger.error("vision_analysis_failed", task_id=task_id, error=str(e))
            return None
        finally:
            # Remove the ephemeral namespace so chat uploads never linger on
            # disk or count against the session image quota.
            if temp_task_id is not None:
                try:
                    await self.cleanup_task_images(temp_task_id)
                except Exception:
                    logger.warning("vision_temp_cleanup_failed", task_id=temp_task_id)

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
