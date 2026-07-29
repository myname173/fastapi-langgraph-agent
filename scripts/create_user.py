r"""Create a user account — for invite-only mode (ALLOW_REGISTRATION=false).

Run inside the app container:

    docker exec <app-container> /app/.venv/bin/python /app/scripts/create_user.py \
        alice@example.com 'Str0ng!Pass' --username Alice

Or from host (requires DB accessible on localhost:5432):

    .venv\Scripts\python.exe scripts\create_user.py alice@example.com 'Str0ng!Pass'

Exits 0 on success, 1 on error (duplicate email, weak password, etc.).
"""

import argparse
import asyncio
import os
import sys

# Ensure the project root (/app or host project dir) is on sys.path so that
# ``from app.* import ...`` works when running from scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import ValidationError

from app.models.user import User
from app.schemas.auth import UserCreate
from app.services.database import DatabaseService


async def create_user(email: str, password: str, username: str | None = None) -> None:
    """Create a user directly in the database (bypasses /register endpoint).

    Args:
        email: User's email address.
        password: Plain-text password (will be validated + hashed).
        username: Optional display name.

    Raises:
        SystemExit: On validation failure or duplicate email.
    """
    db_service = DatabaseService()

    # Validate input via the same Pydantic schema used by /register.
    try:
        user_data = UserCreate(email=email, password=password, username=username)
    except ValidationError as e:
        print(f"Validation error:\n{e}", file=sys.stderr)
        sys.exit(1)

    sanitized_email = user_data.email
    sanitized_username = user_data.username if user_data.username else None

    # Check for existing user.
    existing = await db_service.get_user_by_email(sanitized_email)
    if existing:
        print(f"Error: {sanitized_email} is already registered.", file=sys.stderr)
        sys.exit(1)

    # Create user with hashed password.
    user = await db_service.create_user(
        email=sanitized_email,
        password=User.hash_password(user_data.password.get_secret_value()),
        username=sanitized_username,
    )

    print(f"Created user: id={user.id} email={user.email} username={user.username}")


def main() -> None:
    """Create a user account from CLI arguments (invite-only mode)."""
    parser = argparse.ArgumentParser(
        description="Create a user account (invite-only mode).",
    )
    parser.add_argument("email", help="User's email address")
    parser.add_argument("password", help="Plain-text password (must meet strength policy)")
    parser.add_argument("--username", default=None, help="Optional display name")
    args = parser.parse_args()

    asyncio.run(create_user(args.email, args.password, args.username))


if __name__ == "__main__":
    main()
