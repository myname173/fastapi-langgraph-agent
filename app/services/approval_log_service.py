"""Approval Log Service for audit trail management."""

import hashlib
import json
import uuid
from typing import List, Optional

from sqlmodel import Session, desc, select

from app.core.config import settings
from app.core.db.session import get_async_session_maker
from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.models.approval_log import ApprovalAction, ApprovalLog, ApprovalStep
from app.services.database import database_service


class ApprovalLogService:
    """Service for managing approval audit logs."""

    def _hash_state(self, state) -> str:
        """Generate a hash of the state for verification.

        Args:
            state: State dictionary to hash

        Returns:
            SHA256 hash of the state
        """
        state_json = json.dumps(state, sort_keys=True)
        return hashlib.sha256(state_json.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Sync DB blocks (private) — dispatched off-loop via run_in_thread
    # ------------------------------------------------------------------

    def _log_approval_sync(self, log_entry: ApprovalLog) -> ApprovalLog:
        def _block(session: Session) -> ApprovalLog:
            session.add(log_entry)
            session.commit()
            session.refresh(log_entry)
            return log_entry

        return database_service.run_in_session(_block, op="log_approval")

    def _get_approvals_by_task_sync(self, task_id: str) -> List[ApprovalLog]:
        def _block(session: Session) -> List[ApprovalLog]:
            result = session.exec(
                select(ApprovalLog).where(ApprovalLog.task_id == task_id).order_by(desc(ApprovalLog.timestamp))
            )
            return list(result.all())

        return database_service.run_in_session(_block, op="get_approvals_by_task")

    def _get_approvals_by_user_sync(self, user_id: int) -> List[ApprovalLog]:
        def _block(session: Session) -> List[ApprovalLog]:
            result = session.exec(
                select(ApprovalLog).where(ApprovalLog.user_id == user_id).order_by(desc(ApprovalLog.timestamp))
            )
            return list(result.all())

        return database_service.run_in_session(_block, op="get_approvals_by_user")

    # ------------------------------------------------------------------
    # Native async implementations (used when ENABLE_ASYNC_DB is on)
    # ------------------------------------------------------------------

    async def _log_approval_async(self, log_entry: ApprovalLog) -> ApprovalLog:
        async with get_async_session_maker()() as session:
            session.add(log_entry)
            await session.commit()
            await session.refresh(log_entry)
            return log_entry

    async def _get_approvals_by_task_async(self, task_id: str) -> List[ApprovalLog]:
        async with get_async_session_maker()() as session:
            result = await session.execute(
                select(ApprovalLog).where(ApprovalLog.task_id == task_id).order_by(desc(ApprovalLog.timestamp))
            )
            return list(result.scalars().all())

    async def _get_approvals_by_user_async(self, user_id: int) -> List[ApprovalLog]:
        async with get_async_session_maker()() as session:
            result = await session.execute(
                select(ApprovalLog).where(ApprovalLog.user_id == user_id).order_by(desc(ApprovalLog.timestamp))
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def log_approval(
        self,
        task_id: str,
        step: ApprovalStep,
        action: ApprovalAction,
        user_id: Optional[int] = None,
        reason: Optional[str] = None,
        state=None,
        status_before: Optional[str] = None,
        status_after: Optional[str] = None,
    ) -> ApprovalLog:
        """Log an approval action.

        Args:
            task_id: The task ID
            step: The step being approved/rejected
            action: approve or reject
            user_id: User ID who performed the action
            reason: Optional reason
            state: Optional state snapshot for hashing
            status_before: Status before the action
            status_after: Status after the action

        Returns:
            The created ApprovalLog
        """
        if not settings.ENABLE_AUDIT_LOG:
            return ApprovalLog(
                id="disabled",
                task_id=task_id,
                step=step,
                action=action,
            )

        log_id = str(uuid.uuid4())[:8]
        state_hash = self._hash_state(state) if state else None

        log_entry = ApprovalLog(
            id=log_id,
            task_id=task_id,
            step=step,
            action=action,
            user_id=user_id,
            reason=reason,
            state_hash=state_hash,
            status_before=status_before,
            status_after=status_after,
        )

        if settings.ENABLE_ASYNC_DB:
            log_entry = await self._log_approval_async(log_entry)
        else:
            log_entry = await run_in_thread(self._log_approval_sync, log_entry, op="log_approval")

        logger.info(
            "approval_log_created",
            log_id=log_id,
            task_id=task_id,
            step=step.value,
            action=action.value,
            user_id=user_id,
        )

        return log_entry

    async def get_approvals_by_task(self, task_id: str) -> List[ApprovalLog]:
        """Get all approval logs for a task.

        Args:
            task_id: The task ID

        Returns:
            List of approval logs
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._get_approvals_by_task_async(task_id)
        return await run_in_thread(self._get_approvals_by_task_sync, task_id, op="get_approvals_by_task")

    async def get_approvals_by_user(self, user_id: int) -> List[ApprovalLog]:
        """Get all approval logs for a user.

        Args:
            user_id: The user ID

        Returns:
            List of approval logs
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._get_approvals_by_user_async(user_id)
        return await run_in_thread(self._get_approvals_by_user_sync, user_id, op="get_approvals_by_user")


approval_log_service = ApprovalLogService()
