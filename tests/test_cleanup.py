"""Tests for resource cleanup: scripts, outputs, and task resources."""

import pytest
from unittest.mock import patch, AsyncMock

from app.services.sdlc_task_service import SDLCTaskService


class TestScriptCleanup:
    """Tests for scripts/ and outputs/ directory cleanup."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_workspace, thread_id):
        """Set up test fixtures."""
        self.service = SDLCTaskService()
        self.thread_id = thread_id
        self.workspace = temp_workspace
        self.thread_dir = self.workspace / self.thread_id
        self.thread_dir.mkdir(parents=True, exist_ok=True)

    def test_cleanup_scripts_dir(self):
        """Test that scripts/ directory is cleaned up."""
        scripts_dir = self.thread_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "test.py").write_text("print('test')")
        (scripts_dir / "helper.py").write_text("def help(): pass")

        removed = self.service._cleanup_scripts_and_outputs(self.thread_id)

        assert removed == 2
        assert not scripts_dir.exists()

    def test_cleanup_outputs_dir(self):
        """Test that outputs/ directory is cleaned up."""
        outputs_dir = self.thread_dir / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / "result.txt").write_text("output")
        (outputs_dir / "data").mkdir()
        (outputs_dir / "data" / "values.json").write_text("{}")

        removed = self.service._cleanup_scripts_and_outputs(self.thread_id)

        assert removed == 2
        assert not outputs_dir.exists()

    def test_cleanup_both_dirs(self):
        """Test that both scripts/ and outputs/ are cleaned up."""
        for subdir in ("scripts", "outputs"):
            d = self.thread_dir / subdir
            d.mkdir(parents=True, exist_ok=True)
            (d / "file.txt").write_text("content")

        removed = self.service._cleanup_scripts_and_outputs(self.thread_id)

        assert removed == 2
        assert not (self.thread_dir / "scripts").exists()
        assert not (self.thread_dir / "outputs").exists()

    def test_cleanup_nonexistent_dirs(self):
        """Test that cleanup handles nonexistent directories gracefully."""
        removed = self.service._cleanup_scripts_and_outputs(self.thread_id)
        assert removed == 0

    def test_cleanup_preserves_other_dirs(self):
        """Test that other directories are not affected."""
        images_dir = self.thread_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "photo.png").write_bytes(b"image data")

        scripts_dir = self.thread_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "test.py").write_text("print('test')")

        self.service._cleanup_scripts_and_outputs(self.thread_id)

        assert images_dir.exists()
        assert (images_dir / "photo.png").exists()
        assert not scripts_dir.exists()


class TestCleanupTaskResources:
    """Tests for the full cleanup_task_resources method."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_workspace, thread_id):
        """Set up test fixtures."""
        self.service = SDLCTaskService()
        self.thread_id = thread_id
        self.workspace = temp_workspace

    @pytest.mark.asyncio
    async def test_cleanup_removes_scripts_and_outputs(self):
        """Test that cleanup_task_resources removes scripts and outputs."""
        thread_dir = self.workspace / self.thread_id
        scripts_dir = thread_dir / "scripts"
        outputs_dir = thread_dir / "outputs"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "gen.py").write_text("print('gen')")
        (outputs_dir / "out.txt").write_text("result")

        with patch.object(self.service, "_cleanup_scripts_and_outputs", return_value=2) as mock_cleanup:
            with patch("app.services.sdlc_task_service.image_service") as mock_img:
                mock_img.cleanup_task_images = AsyncMock(return_value=0)
                with patch("app.services.sdlc_task_service.knowledge_graph_service") as mock_kg:
                    mock_kg.cleanup_graph = AsyncMock(return_value=True)

                    await self.service.cleanup_task_resources(self.thread_id)

                    mock_cleanup.assert_called_once_with(self.thread_id)
                    mock_img.cleanup_task_images.assert_called_once_with(self.thread_id)
                    mock_kg.cleanup_graph.assert_called_once_with(self.thread_id)
