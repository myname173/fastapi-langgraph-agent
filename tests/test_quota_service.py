"""Tests for quota_service: workspace, image count, and task count quotas."""

import pytest

from app.services.quota_service import QuotaService


class TestWorkspaceQuota:
    """Tests for workspace size quota checking."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_workspace, enable_resource_quota, thread_id):
        """Set up test fixtures."""
        self.service = QuotaService()
        self.thread_id = thread_id
        self.workspace = temp_workspace

    def test_workspace_within_quota(self):
        """Test that empty workspace passes quota check."""
        assert self.service.check_workspace_quota(self.thread_id) is True

    def test_workspace_exceeds_quota(self, small_workspace_quota):
        """Test that workspace exceeding quota is rejected."""
        thread_dir = self.workspace / self.thread_id
        thread_dir.mkdir(parents=True, exist_ok=True)
        large_file = thread_dir / "large.bin"
        large_file.write_bytes(b"x" * (2 * 1024 * 1024))

        assert self.service.check_workspace_quota(self.thread_id) is False

    def test_workspace_quota_disabled(self, disable_resource_quota):
        """Test that quota check passes when disabled."""
        assert self.service.check_workspace_quota(self.thread_id) is True

    def test_workspace_nonexistent_dir(self):
        """Test that nonexistent workspace passes (no files to count)."""
        assert self.service.check_workspace_quota("nonexistent-thread") is True


class TestImageCountQuota:
    """Tests for image count quota checking."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_workspace, enable_resource_quota, thread_id):
        """Set up test fixtures."""
        self.service = QuotaService()
        self.thread_id = thread_id
        self.workspace = temp_workspace

    def test_no_images_passes(self):
        """Test that no images passes quota."""
        assert self.service.check_image_count_quota(self.thread_id) is True

    def test_within_image_limit(self):
        """Test that images under limit pass."""
        from app.core.config import settings

        image_dir = self.workspace / self.thread_id / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        for i in range(settings.MAX_IMAGES_PER_TASK - 1):
            (image_dir / f"img_{i}.png").write_bytes(b"fake image")

        assert self.service.check_image_count_quota(self.thread_id) is True

    def test_exceeds_image_limit(self):
        """Test that exceeding image limit is rejected."""
        from app.core.config import settings

        image_dir = self.workspace / self.thread_id / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        for i in range(settings.MAX_IMAGES_PER_TASK):
            (image_dir / f"img_{i}.png").write_bytes(b"fake image")

        assert self.service.check_image_count_quota(self.thread_id) is False

    def test_image_quota_disabled(self, disable_resource_quota):
        """Test that image quota passes when disabled."""
        assert self.service.check_image_count_quota(self.thread_id) is True


class TestTaskCountQuota:
    """Tests for task count quota checking."""

    @pytest.fixture(autouse=True)
    def setup(self, enable_resource_quota):
        """Set up test fixtures."""
        self.service = QuotaService()

    @pytest.mark.asyncio
    async def test_no_user_passes(self):
        """Test that None user_id passes."""
        assert await self.service.check_task_count_quota(None) is True

    @pytest.mark.asyncio
    async def test_quota_disabled(self, disable_resource_quota):
        """Test that task quota passes when disabled."""
        assert await self.service.check_task_count_quota(1) is True
