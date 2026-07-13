"""Tests for image_service: caching, validation, and cleanup."""

import io
import time
import pytest
from unittest.mock import patch

from PIL import Image

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
