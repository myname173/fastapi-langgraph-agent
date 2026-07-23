"""This file contains the chat schema for the application."""

import re
from typing import (
    List,
    Literal,
    Optional,
)

from pydantic import (
    BaseModel,
    Field,
    field_validator,
)

from app.schemas.base import BaseResponse


class Message(BaseModel):
    """Message model for chat endpoint.

    Attributes:
        role: The role of the message sender (user or assistant).
        content: The content of the message.
    """

    model_config = {"extra": "ignore"}

    role: Literal["user", "assistant", "system"] = Field(..., description="The role of the message sender")
    content: str = Field(..., description="The content of the message", min_length=1, max_length=3000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        """Validate the message content.

        Args:
            v: The content to validate

        Returns:
            str: The validated content

        Raises:
            ValueError: If the content contains disallowed patterns
        """
        # Check for potentially harmful content
        if re.search(r"<script.*?>.*?</script>", v, re.IGNORECASE | re.DOTALL):
            raise ValueError("Content contains potentially harmful script tags")

        # Check for null bytes
        if "\0" in v:
            raise ValueError("Content contains null bytes")

        return v


class ChatImage(BaseModel):
    """A single image attached to a chat message.

    Only ``url`` (http/https) and ``base64`` (data:image/...;base64,...) are
    accepted. The number of images per request is capped by ``ChatRequest``.
    """

    model_config = {"extra": "ignore"}

    type: Literal["url", "base64"] = "url"
    data: str = Field(..., description="http(s) URL or a data:image/...;base64,... string")

    @field_validator("data")
    @classmethod
    def _validate_data(cls, v: str) -> str:
        """Validate that ``data`` is a URL or a base64 image data URL.

        Args:
            v: The raw image data string.

        Returns:
            str: The validated data string.

        Raises:
            ValueError: If the data is empty or of an unsupported scheme.
        """
        if not v:
            raise ValueError("image data must not be empty")
        if v.startswith("data:"):
            if not v.startswith("data:image/") or ";base64," not in v:
                raise ValueError("base64 image must be a data:image/...;base64,... string")
        elif not v.startswith(("http://", "https://")):
            raise ValueError("image data must be a URL (http/https) or a data:image base64 string")
        return v


class InvestmentImageAnalysis(BaseModel):
    """Structured result of investment-image analysis by the vision model.

    Designed to describe only (no buy/sell advice) for chat investment use.
    """

    image_id: str = Field(..., description="图片标识")
    description: str = Field(..., description="图片内容与关键要素的通俗描述")
    key_elements: List[str] = Field(default_factory=list, description="关键要素列表")


class ChatRequest(BaseModel):
    """Request model for chat endpoint.

    Attributes:
        messages: List of messages in the conversation.
        images: Optional images attached to the latest user message. Recognized
            only when ``ENABLE_MULTIMODAL`` is on; otherwise ignored server-side.
    """

    messages: List[Message] = Field(
        ...,
        description="List of messages in the conversation",
        min_length=1,
    )
    images: Optional[List[ChatImage]] = Field(
        default=None,
        max_length=4,
        description="Optional images for the latest user message (max 4)",
    )


class ChatResponse(BaseResponse):
    """Response model for chat endpoint.

    Attributes:
        messages: List of messages in the conversation.
    """

    messages: List[Message] = Field(..., description="List of messages in the conversation")


class StreamResponse(BaseResponse):
    """Response model for streaming chat endpoint.

    Attributes:
        content: The content of the current chunk.
        done: Whether the stream is complete.
    """

    content: str = Field(default="", description="The content of the current chunk")
    done: bool = Field(default=False, description="Whether the stream is complete")


class SessionTitle(BaseModel):
    """Structured output schema for session title generation."""

    title: str = Field(
        ...,
        min_length=1,
        max_length=60,
    )

    @field_validator("title")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = " ".join(v.split()).strip(" \"'`.,:;!?-")
        if not v:
            raise ValueError("empty title after normalization")
        return v
