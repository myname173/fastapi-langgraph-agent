"""Alert service — price-breakout triggers backed by database_service."""

from __future__ import annotations

import uuid
from datetime import datetime, UTC
from typing import Sequence

from sqlmodel import Session, select

from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.models.alert import Alert, AlertCondition
from app.services.database import database_service


class AlertService:
    """User-defined price-breakout alerts (价格预警)."""

    # ------------------------------------------------------------------
    # Sync DB blocks
    # ------------------------------------------------------------------

    def _list_sync(self, user_id: int, active_only: bool) -> Sequence[Alert]:
        def _block(session: Session) -> Sequence[Alert]:
            stmt = select(Alert).where(Alert.user_id == user_id)
            if active_only:
                stmt = stmt.where(Alert.active == True)  # noqa: E712
            return session.exec(stmt.order_by(Alert.created_at.desc())).all()  # pyright: ignore[reportAttributeAccessIssue]

        return database_service.run_in_session(_block, op="alert_list")

    def _create_sync(
        self,
        user_id: int,
        symbol: str,
        name: str,
        condition: AlertCondition,
        price: float,
        note: str,
    ) -> Alert:
        def _block(session: Session) -> Alert:
            alert = Alert(
                id=str(uuid.uuid4()),
                user_id=user_id,
                symbol=symbol,
                name=name,
                condition=condition,
                price=price,
                note=note,
            )
            session.add(alert)
            session.commit()
            session.refresh(alert)
            logger.info("alert_created", user_id=user_id, symbol=symbol)
            return alert

        return database_service.run_in_session(_block, op="alert_create")

    def _update_sync(
        self,
        alert_id: str,
        user_id: int,
        active: bool | None,
        price: float | None,
        condition: AlertCondition | None,
        note: str | None,
    ) -> Alert | None:
        def _block(session: Session) -> Alert | None:
            alert = session.get(Alert, alert_id)
            if not alert or alert.user_id != user_id:
                return None
            if active is not None:
                alert.active = active
            if price is not None:
                alert.price = price
            if condition is not None:
                alert.condition = condition
            if note is not None:
                alert.note = note
            alert.updated_at = datetime.now(UTC)
            session.add(alert)
            session.commit()
            session.refresh(alert)
            return alert

        return database_service.run_in_session(_block, op="alert_update")

    def _fire_sync(self, alert_id: str, user_id: int) -> Alert | None:
        def _block(session: Session) -> Alert | None:
            alert = session.get(Alert, alert_id)
            if not alert or alert.user_id != user_id or not alert.active:
                return None
            alert.active = False
            alert.fired_at = datetime.now(UTC)
            alert.updated_at = datetime.now(UTC)
            session.add(alert)
            session.commit()
            session.refresh(alert)
            logger.info("alert_fired", alert_id=alert_id, user_id=user_id)
            return alert

        return database_service.run_in_session(_block, op="alert_fire")

    def _delete_sync(self, alert_id: str, user_id: int) -> bool:
        def _block(session: Session) -> bool:
            alert = session.get(Alert, alert_id)
            if not alert or alert.user_id != user_id:
                return False
            session.delete(alert)
            session.commit()
            return True

        return database_service.run_in_session(_block, op="alert_delete")

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def list(self, *, user_id: int, active_only: bool = False) -> Sequence[Alert]:
        """List alerts for a user, optionally filtering to active only.

        Args:
            user_id: Owning user id.
            active_only: When True, return only enabled alerts.

        Returns:
            Matching ``Alert`` rows ordered by creation time (desc).
        """
        return await run_in_thread(self._list_sync, user_id, active_only, op="alert_list")

    async def create(
        self,
        *,
        user_id: int,
        symbol: str,
        name: str = "",
        condition: AlertCondition = AlertCondition.ABOVE,
        price: float = 0.0,
        note: str = "",
    ) -> Alert:
        """Create a new price-breakout alert.

        Args:
            user_id: Owning user id.
            symbol: Ticker symbol to watch.
            name: Optional human-friendly label.
            condition: Trigger direction (above/below).
            price: Threshold price.
            note: Optional free-form note.

        Returns:
            The newly persisted ``Alert``.
        """
        return await run_in_thread(self._create_sync, user_id, symbol, name, condition, price, note, op="alert_create")

    async def update(
        self,
        *,
        alert_id: str,
        user_id: int,
        active: bool | None = None,
        price: float | None = None,
        condition: AlertCondition | None = None,
        note: str | None = None,
    ) -> Alert | None:
        """Update mutable fields of an existing alert.

        Args:
            alert_id: Target alert id.
            user_id: Owning user id (ownership check enforced).
            active: Optional enable/disable toggle.
            price: Optional new threshold.
            condition: Optional new direction.
            note: Optional new note.

        Returns:
            Updated ``Alert`` or None if not found / not owned.
        """
        return await run_in_thread(
            self._update_sync,
            alert_id,
            user_id,
            active,
            price,
            condition,
            note,
            op="alert_update",
        )

    async def fire(self, *, alert_id: str, user_id: int) -> Alert | None:
        """Mark an active alert as fired (one-shot disable).

        Args:
            alert_id: Target alert id.
            user_id: Owning user id (ownership + active check enforced).

        Returns:
            The fired ``Alert`` or None if missing / not owned / inactive.
        """
        return await run_in_thread(self._fire_sync, alert_id, user_id, op="alert_fire")

    async def delete(self, *, alert_id: str, user_id: int) -> bool:
        """Delete an alert owned by the user.

        Args:
            alert_id: Target alert id.
            user_id: Owning user id (ownership check enforced).

        Returns:
            True if deleted, False if not found / not owned.
        """
        return await run_in_thread(self._delete_sync, alert_id, user_id, op="alert_delete")


alert_service = AlertService()
