"""Read/write service for content-free LLM execution audit records."""

import uuid
from typing import Optional

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db.session import get_async_session_maker
from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.models.llm_execution import LLMExecution
from app.services.database import database_service


class LLMExecutionService:
    """Best-effort persistence for model attempts; auditing never breaks a task."""

    def _record_sync(self, entry: LLMExecution) -> None:
        def _block(session: Session) -> None:
            session.add(entry)
            session.commit()

        database_service.run_in_session(_block, op="llm_execution_record")

    async def record(
        self,
        *,
        execution_id: Optional[str],
        user_id: Optional[int],
        agent_name: str,
        purpose: str,
        requested_model: str,
        effective_model: str,
        actual_model: str,
        deployment: str,
        attempt: int,
        outcome: str,
        duration_ms: int,
        error_type: Optional[str] = None,
    ) -> None:
        """Persist one attempt when an execution id is available."""
        if not settings.ENABLE_LLM_EXECUTION_AUDIT:
            return
        if not execution_id:
            # Auditing must never break a task, so this stays a no-op — but it
            # must not be *silent*. SDLC agent calls produced zero audit rows
            # for months because the node wrapper was not receiving `config`
            # (LangGraph only injects it when the parameter is annotated
            # `RunnableConfig`), so `thread_id` was always None here. Without
            # this log the only symptom was an empty UI panel.
            logger.debug(
                "llm_execution_audit_skipped_no_execution_id",
                agent_name=agent_name,
                purpose=purpose,
                effective_model=effective_model,
            )
            return
        entry = LLMExecution(
            id=str(uuid.uuid4()),
            execution_id=execution_id,
            user_id=user_id,
            agent_name=agent_name,
            purpose=purpose,
            requested_model=requested_model,
            effective_model=effective_model,
            actual_model=actual_model,
            deployment=deployment,
            attempt=attempt,
            outcome=outcome,
            duration_ms=duration_ms,
            error_type=error_type,
        )
        try:
            if settings.ENABLE_ASYNC_DB:
                async with get_async_session_maker()() as session:
                    session.add(entry)
                    await session.commit()
            else:
                await run_in_thread(self._record_sync, entry, op="llm_execution_record")
        except Exception as exc:
            logger.warning("llm_execution_audit_write_failed", error_type=type(exc).__name__)

    def _get_for_execution_sync(self, execution_id: str) -> list[LLMExecution]:
        def _block(session: Session) -> list[LLMExecution]:
            result = session.exec(
                select(LLMExecution)
                .where(LLMExecution.execution_id == execution_id)
                .order_by(col(LLMExecution.created_at))
            )
            return list(result.all())

        return database_service.run_in_session(_block, op="llm_execution_get")

    async def get_for_execution(self, execution_id: str) -> list[LLMExecution]:
        """Return all model attempts for an execution in chronological order."""
        if settings.ENABLE_ASYNC_DB:
            async with get_async_session_maker()() as session:
                result = await session.execute(
                    select(LLMExecution)
                    .where(LLMExecution.execution_id == execution_id)
                    .order_by(col(LLMExecution.created_at))
                )
                return list(result.scalars().all())
        return await run_in_thread(self._get_for_execution_sync, execution_id, op="llm_execution_get")


llm_execution_service = LLMExecutionService()
