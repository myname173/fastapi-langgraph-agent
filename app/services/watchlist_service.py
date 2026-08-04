"""Watchlist service — async CRUD backed by database_service.run_in_session.

Mirrors ``portfolio_service.py``: sync-DB blocks dispatched via
``run_in_thread`` + ``database_service.run_in_session``.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlmodel import Session, select

from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.models.watchlist import WatchlistItem
from app.services.database import database_service


class WatchlistService:
    """User-curated stock watchlist (自选股)."""

    # ------------------------------------------------------------------
    # Sync DB blocks (private) — run on worker thread
    # ------------------------------------------------------------------

    def _list_sync(self, user_id: int) -> Sequence[WatchlistItem]:
        def _block(session: Session) -> Sequence[WatchlistItem]:
            return session.exec(
                select(WatchlistItem).where(WatchlistItem.user_id == user_id).order_by(WatchlistItem.created_at.desc())  # pyright: ignore[reportAttributeAccessIssue]
            ).all()

        return database_service.run_in_session(_block, op="watchlist_list")

    def _add_sync(self, user_id: int, symbol: str, name: str, notes: str) -> WatchlistItem:
        def _block(session: Session) -> WatchlistItem:
            item = WatchlistItem(
                id=str(uuid.uuid4()),
                user_id=user_id,
                symbol=symbol,
                name=name,
                notes=notes,
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            logger.info("watchlist_item_added", user_id=user_id, symbol=symbol)
            return item

        return database_service.run_in_session(_block, op="watchlist_add")

    def _remove_sync(self, item_id: str, user_id: int) -> bool:
        def _block(session: Session) -> bool:
            item = session.get(WatchlistItem, item_id)
            if not item or item.user_id != user_id:
                return False
            session.delete(item)
            session.commit()
            logger.info("watchlist_item_removed", item_id=item_id, user_id=user_id)
            return True

        return database_service.run_in_session(_block, op="watchlist_remove")

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def list(self, *, user_id: int) -> Sequence[WatchlistItem]:
        """List watchlist items for a user.

        Args:
            user_id: Owning user id.

        Returns:
            ``WatchlistItem`` rows ordered by creation time (desc).
        """
        return await run_in_thread(self._list_sync, user_id, op="watchlist_list")

    async def add(self, *, user_id: int, symbol: str, name: str = "", notes: str = "") -> WatchlistItem:
        """Add a symbol to the user's watchlist.

        Args:
            user_id: Owning user id.
            symbol: Ticker symbol to add.
            name: Optional label.
            notes: Optional free-form notes.

        Returns:
            The newly persisted ``WatchlistItem``.
        """
        return await run_in_thread(self._add_sync, user_id, symbol, name, notes, op="watchlist_add")

    async def remove(self, *, item_id: str, user_id: int) -> bool:
        """Remove a watchlist item owned by the user.

        Args:
            item_id: Target item id.
            user_id: Owning user id (ownership check enforced).

        Returns:
            True if removed, False if not found / not owned.
        """
        return await run_in_thread(self._remove_sync, item_id, user_id, op="watchlist_remove")


watchlist_service = WatchlistService()
