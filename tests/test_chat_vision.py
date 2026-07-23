"""Tests for chat vision (Route B): schema, prompt injection, quota, and resolution.

These are unit-level tests that avoid DB / network by mocking the vision
model service and patching ``settings``. They cover the Phase 1 chat image
feature end-to-end at the boundary of the orchestration layer.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import settings
from app.core.langgraph.graph import LangGraphAgent
from app.schemas.chat import ChatImage, ChatRequest, InvestmentImageAnalysis
from app.services.quota_service import QuotaService


def _image(url: str = "https://example.com/a.png") -> ChatImage:
    """Build a chat image for tests."""
    return ChatImage(type="url", data=url)


class TestChatVisionSchema:
    """ChatRequest must accept optional images and validate them."""

    def test_images_optional(self):
        """Images are optional — plain-text chat still works."""
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}])
        assert req.images is None

    def test_valid_url_image(self):
        """An http(s) URL image is accepted."""
        req = ChatRequest(
            messages=[{"role": "user", "content": "看图"}],
            images=[{"type": "url", "data": "https://example.com/a.png"}],
        )
        assert req.images[0].type == "url"

    def test_valid_base64_image(self):
        """A data:image base64 image is accepted."""
        data = "data:image/png;base64,iVBORw0KGgo="
        req = ChatRequest(
            messages=[{"role": "user", "content": "x"}],
            images=[{"type": "base64", "data": data}],
        )
        assert req.images[0].type == "base64"

    def test_invalid_scheme_rejected(self):
        """Non-URL / non-data schemes are rejected."""
        with pytest.raises(Exception):
            ChatRequest(
                messages=[{"role": "user", "content": "x"}],
                images=[{"type": "url", "data": "ftp://example.com/a.png"}],
            )

    def test_too_many_images_rejected(self):
        """More than 4 images are rejected by ``max_length``."""
        imgs = [{"type": "url", "data": f"https://e.com/{i}.png"} for i in range(5)]
        with pytest.raises(Exception):
            ChatRequest(messages=[{"role": "user", "content": "x"}], images=imgs)


class TestLoadSystemPromptVision:
    """The vision context must be injected via the sentinel safely."""

    def test_vision_injected(self):
        from app.core.prompts import load_system_prompt

        out = load_system_prompt(username="alice", long_term_memory="mem", vision_context="# VISION")
        assert "# VISION" in out
        assert "<<VISION_CONTEXT>>" not in out

    def test_vision_braces_safe(self):
        """Curly braces inside vision content must not raise on .format."""
        from app.core.prompts import load_system_prompt

        out = load_system_prompt(username="alice", long_term_memory="mem", vision_context="a {b} c")
        assert "a {b} c" in out

    def test_empty_removes_sentinel(self):
        """When no vision context, the sentinel must be stripped."""
        from app.core.prompts import load_system_prompt

        out = load_system_prompt(username="alice", long_term_memory="mem")
        assert "<<VISION_CONTEXT>>" not in out


class TestVisionQuota:
    """Daily per-user vision quota mirrors the media-quota pattern."""

    @pytest.fixture(autouse=True)
    def setup(self, enable_resource_quota):
        self.service = QuotaService()

    def test_anonymous_passes(self):
        assert self.service.check_vision_quota(None) is True

    def test_increments_and_blocks(self):
        with patch.object(settings, "MAX_VISION_ANALYSES_PER_USER_PER_DAY", 2):
            assert self.service.check_vision_quota(1) is True
            self.service.increment_vision_usage(1)
            assert self.service.check_vision_quota(1) is True
            self.service.increment_vision_usage(1)
            assert self.service.check_vision_quota(1) is False

    def test_disabled_passes(self, disable_resource_quota):
        assert self.service.check_vision_quota(1) is True


class TestResolveVisionContext:
    """_resolve_vision_context degrades gracefully and respects quota."""

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        """Multimodal off → no vision analysis, images ignored."""
        svc = MagicMock()
        with patch.object(settings, "ENABLE_MULTIMODAL", False), patch("app.core.langgraph.graph.image_service", svc):
            result = await LangGraphAgent._resolve_vision_context(object(), [_image()], "1", "s1")
        assert result is None
        svc.analyze_investment_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_vlm_failure_returns_none(self):
        """VLM unreachable / returns None → degrade to plain text."""
        svc = MagicMock()
        svc.analyze_investment_image = AsyncMock(return_value=None)
        qs = QuotaService()
        with (
            patch.object(settings, "ENABLE_MULTIMODAL", True),
            patch("app.core.langgraph.graph.image_service", svc),
            patch("app.core.langgraph.graph.quota_service", qs),
            patch.object(settings, "MAX_VISION_ANALYSES_PER_USER_PER_DAY", 30),
        ):
            result = await LangGraphAgent._resolve_vision_context(object(), [_image()], "1", "s1")
        assert result is None

    @pytest.mark.asyncio
    async def test_success_concatenates_and_counts(self):
        """Successful analysis → concatenated block + usage incremented."""
        svc = MagicMock()
        svc.analyze_investment_image = AsyncMock(
            return_value=InvestmentImageAnalysis(
                image_id="i1",
                description="K线图显示上涨，伴随放量",
                key_elements=["MA5上穿MA10", "成交量放大"],
            )
        )
        qs = QuotaService()
        with (
            patch.object(settings, "ENABLE_MULTIMODAL", True),
            patch("app.core.langgraph.graph.image_service", svc),
            patch("app.core.langgraph.graph.quota_service", qs),
            patch.object(settings, "MAX_VISION_ANALYSES_PER_USER_PER_DAY", 30),
        ):
            result = await LangGraphAgent._resolve_vision_context(object(), [_image()], "1", "s1")
        assert result is not None
        assert "K线图显示上涨" in result
        assert "MA5上穿MA10" in result
        svc.analyze_investment_image.assert_awaited_once()
        # Usage was incremented: with cap 0 it should now be exhausted.
        with patch.object(settings, "MAX_VISION_ANALYSES_PER_USER_PER_DAY", 0):
            assert qs.check_vision_quota("1") is False

    @pytest.mark.asyncio
    async def test_quota_exceeded_skips_analysis(self):
        """Over quota → skip vision analysis entirely (no VLM call)."""
        svc = MagicMock()
        svc.analyze_investment_image = AsyncMock(return_value=None)
        qs = QuotaService()
        with (
            patch.object(settings, "ENABLE_MULTIMODAL", True),
            patch("app.core.langgraph.graph.image_service", svc),
            patch("app.core.langgraph.graph.quota_service", qs),
            patch.object(settings, "MAX_VISION_ANALYSES_PER_USER_PER_DAY", 0),
        ):
            result = await LangGraphAgent._resolve_vision_context(object(), [_image()], "1", "s1")
        assert result is None
        svc.analyze_investment_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_temp_namespace(self):
        """Chat vision must use an ephemeral namespace so session uploads
        don't accumulate or exhaust MAX_IMAGES_PER_TASK.
        """
        svc = MagicMock()
        svc.analyze_investment_image = AsyncMock(
            return_value=InvestmentImageAnalysis(image_id="i1", description="desc", key_elements=[])
        )
        qs = QuotaService()
        with (
            patch.object(settings, "ENABLE_MULTIMODAL", True),
            patch("app.core.langgraph.graph.image_service", svc),
            patch("app.core.langgraph.graph.quota_service", qs),
            patch.object(settings, "MAX_VISION_ANALYSES_PER_USER_PER_DAY", 30),
        ):
            result = await LangGraphAgent._resolve_vision_context(object(), [_image()], "1", "s1")
        assert result is not None
        _, kwargs = svc.analyze_investment_image.call_args
        assert kwargs.get("temp") is True
        # The session id is no longer used as the image namespace.
        assert "task_id" not in kwargs
