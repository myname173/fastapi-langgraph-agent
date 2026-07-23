"""Tests for image_service: caching, validation, and cleanup."""

import base64
import io
import os
import tempfile
import time
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.schemas.chat import InvestmentImageAnalysis
from app.services.image_service import ImageService


def create_minimal_png() -> bytes:
    """Create a minimal valid PNG image."""
    img = Image.new("RGB", (10, 10), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def create_minimal_jpeg() -> bytes:
    """Create a minimal valid JPEG image."""
    img = Image.new("RGB", (10, 10), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestImageAnalysisCache:
    """Tests for image analysis result caching."""

    @pytest.fixture
    def service(self):
        """Create image service instance."""
        return ImageService()

    def test_cache_key_generation(self, service):
        """Test that cache keys are deterministic for same inputs."""
        key1 = service._get_cache_key("img_001", "describe UI")
        key2 = service._get_cache_key("img_001", "describe UI")
        key3 = service._get_cache_key("img_001", "describe layout")

        assert key1 == key2
        assert key1 != key3

    def test_cache_key_different_images(self, service):
        """Test that different images get different cache keys."""
        key1 = service._get_cache_key("img_001", "query")
        key2 = service._get_cache_key("img_002", "query")

        assert key1 != key2

    def test_cache_cleanup_removes_expired(self, service):
        """Test that expired cache entries are removed."""
        service._analysis_cache = {
            "key1": {"result": {"description": "old"}, "timestamp": time.time() - 9999},
            "key2": {"result": {"description": "new"}, "timestamp": time.time()},
        }

        with patch("app.core.config.settings.IMAGE_ANALYSIS_CACHE_TTL_SECONDS", 60):
            service._clean_analysis_cache()

        assert "key1" not in service._analysis_cache
        assert "key2" in service._analysis_cache

    def test_cache_cleanup_keeps_valid(self, service):
        """Test that valid cache entries are kept."""
        service._analysis_cache = {
            "key1": {"result": {"description": "valid"}, "timestamp": time.time()},
        }

        service._clean_analysis_cache()

        assert "key1" in service._analysis_cache

    def test_cache_cleanup_empty_cache(self, service):
        """Test that cleanup handles empty cache gracefully."""
        service._analysis_cache = {}
        service._clean_analysis_cache()
        assert len(service._analysis_cache) == 0


class TestImageValidation:
    """Tests for image validation logic."""

    @pytest.fixture
    def service(self, enable_multimodal):
        """Create image service instance with multimodal enabled."""
        return ImageService()

    def test_validate_valid_png(self, service):
        """Test that valid PNG data passes validation."""
        is_valid, error = service._validate_image(create_minimal_png())
        assert is_valid is True
        assert error is None

    def test_validate_valid_jpeg(self, service):
        """Test that valid JPEG data passes validation."""
        is_valid, error = service._validate_image(create_minimal_jpeg())
        assert is_valid is True
        assert error is None

    def test_validate_invalid_format(self, service):
        """Test that invalid format is rejected."""
        invalid_data = b"NOT_AN_IMAGE" + b"\x00" * 100
        is_valid, error = service._validate_image(invalid_data)
        assert is_valid is False
        assert error is not None

    def test_validate_empty_data(self, service):
        """Test that empty data is rejected."""
        is_valid, error = service._validate_image(b"")
        assert is_valid is False

    def test_validate_oversized_image(self, service):
        """Test that oversized image is rejected."""
        service._max_size_bytes = 10
        is_valid, error = service._validate_image(create_minimal_png())
        assert is_valid is False


class TestInvestmentVisionTempNamespace:
    """Chat vision must use an ephemeral namespace that is cleaned up (P2 fix).

    Previously chat uploads were namespaced under the session id, so they
    accumulated in the session workspace and exhausted ``MAX_IMAGES_PER_TASK``
    over a long conversation, silently dropping later images. They are now
    stored under a throwaway per-request namespace and deleted after analysis.
    """

    @pytest.fixture
    def service(self, enable_multimodal):
        return ImageService()

    async def _run(self, service, temp: bool) -> tuple[list, list]:
        """Drive analyze_investment_image with a real readable PNG.

        Returns the recorded (saved_task_ids, cleaned_task_ids) so the test
        can assert which namespace was used and whether cleanup happened.
        """
        png = create_minimal_png()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(png)
        tmp.close()
        fake_path = Path(tmp.name)

        saved: list = []
        cleaned: list = []

        async def fake_save(image_bytes, task_id):
            saved.append(task_id)
            return "img-1"

        async def fake_cleanup(task_id):
            cleaned.append(task_id)
            return 1

        analysis = InvestmentImageAnalysis(image_id="img-1", description="desc", key_elements=[])
        with (
            patch.object(service, "save_uploaded_image", side_effect=fake_save),
            patch.object(service, "cleanup_task_images", side_effect=fake_cleanup),
            patch.object(service, "get_image_path", return_value=fake_path),
            patch("app.services.image_service.LLMRegistry") as reg,
            patch("app.services.image_service.llm_service") as llm,
        ):
            reg.ensure_model.return_value = None
            llm.call = AsyncMock(return_value=analysis)
            data_url = "data:image/png;base64," + base64.b64encode(png).decode()
            result = await service.analyze_investment_image(data_url, task_id="sess-9", user_id=7, temp=temp)

        os.unlink(tmp.name)
        assert result is analysis
        return saved, cleaned

    @pytest.mark.asyncio
    async def test_temp_namespace_used_and_cleaned(self, service):
        """temp=True stores under chat-tmp-* and cleans it up afterwards."""
        saved, cleaned = await self._run(service, temp=True)
        assert len(saved) == 1
        assert saved[0].startswith("chat-tmp-")
        assert len(cleaned) == 1
        assert cleaned[0] == saved[0]

    @pytest.mark.asyncio
    async def test_non_temp_keeps_task_id(self, service):
        """Without temp=True the supplied task_id is used and nothing is cleaned."""
        saved, cleaned = await self._run(service, temp=False)
        assert saved[0] == "sess-9"
        assert cleaned == []
