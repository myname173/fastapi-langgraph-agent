"""This file contains the authentication schema for the application."""

import re
from datetime import datetime

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
)

from app.utils.sanitization import validate_password_strength

from app.models.base import UTCDateTimeMixin
from app.schemas.base import BaseResponse


class Token(UTCDateTimeMixin, BaseModel):
    """Token model for authentication.

    Attributes:
        access_token: The JWT access token.
        token_type: The type of token (always "bearer").
        expires_at: The token expiration timestamp.
    """

    access_token: str = Field(..., description="The JWT access token")
    token_type: str = Field(default="bearer", description="The type of token")
    expires_at: datetime = Field(..., description="The token expiration timestamp")


class TokenResponse(BaseResponse):
    """Response model for login endpoint.

    Attributes:
        access_token: The JWT access token
        token_type: The type of token (always "bearer")
        expires_at: When the token expires
    """

    access_token: str = Field(..., description="The JWT access token")
    token_type: str = Field(default="bearer", description="The type of token")
    expires_at: datetime = Field(..., description="When the token expires")


class UserCreate(BaseModel):
    """Request model for user registration.

    Attributes:
        email: User's email address
        password: User's password
        username: Optional display name
    """

    email: EmailStr = Field(..., description="User's email address")
    password: SecretStr = Field(..., description="User's password", min_length=8, max_length=64)
    username: str | None = Field(default=None, description="Optional display name", max_length=50)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: SecretStr) -> SecretStr:
        """Validate password strength.

        Delegates to :func:`app.utils.sanitization.validate_password_strength`
        so there is a single source of truth for the password policy (L-5
        fix — previously the rules were duplicated here and re-run in the
        register endpoint).

        Args:
            v: The password to validate

        Returns:
            SecretStr: The validated password

        Raises:
            ValueError: If the password is not strong enough
        """
        # validate_password_strength raises ValueError on failure; Pydantic
        # converts that into a 422 validation error automatically.
        validate_password_strength(v.get_secret_value())
        return v


class UserResponse(BaseResponse):
    """Response model for user operations.

    Attributes:
        id: User's ID
        email: User's email address
        username: Optional display name
        token: Authentication token
    """

    id: int = Field(..., description="User's ID")
    email: str = Field(..., description="User's email address")
    username: str | None = Field(default=None, description="Optional display name")
    token: Token = Field(..., description="Authentication token")


class SessionResponse(BaseResponse):
    """Response model for session creation.

    Attributes:
        session_id: The unique identifier for the chat session
        name: Name of the session (defaults to empty string)
        token: The authentication token for the session. Only populated by
            ``create_session`` and ``resume_session``; list/update endpoints
            leave it ``None`` to avoid minting unused JWTs on every call
            (L-6 fix).
        created_at: When the session was created. Populated by the list
            endpoint so the chat-history sidebar can order/label sessions.
    """

    session_id: str = Field(..., description="The unique identifier for the chat session")
    name: str = Field(default="", description="Name of the session", max_length=100)
    token: Token | None = Field(
        default=None, description="The authentication token for the session (only set on creation)"
    )
    created_at: datetime | None = Field(
        default=None, description="When the session was created (set by the list endpoint)"
    )

    @field_validator("name")
    @classmethod
    def sanitize_name(cls, v: str) -> str:
        """Sanitize the session name.

        Args:
            v: The name to sanitize

        Returns:
            str: The sanitized name
        """
        # Remove any potentially harmful characters
        sanitized = re.sub(r'[<>{}[\]()\'"`]', "", v)
        return sanitized
