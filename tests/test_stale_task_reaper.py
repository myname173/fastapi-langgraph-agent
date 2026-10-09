"""Tests for the SDLC stale-task reaper and ``coder_model`` persistence.

Background: an interrupted SDLC run (process restart, crash, aborted SSE
stream) never writes a terminal status, so its ``sdlc_task`` row sits in
「进行中」 forever — nothing else in the system transitions it. Production had
tasks stranded for weeks (``PLANNING`` since 08-12, ``REVIEWING`` since 08-06).

A subtlety worth knowing when reading these tests: the ``status`` column is a
SQLAlchemy ``Enum`` whose value set is the member **names** (``'PLANNING'``),
while the API serialises the member **values** (``'planning'``). Both spellings
match through the ORM, because the Enum bind processor normalises them to the
name — so these tests pass with either form. (Verified against the real
Postgres DB: members, ``.name`` and ``.value`` all match the same 9 rows.
Raw SQL is *not* so forgiving — lowercase literals match 0 rows there.)
"""

from datetime import datetime, timedelta, UTC
from unittest.mock import patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.sdlc_task import SDLCTask, SDLCTaskStatus
from app.services.database import database_service
from app.services.sdlc_task_service import SDLCTaskService


@pytest.fixture
def engine():
    """In-memory SQLite with just the ``sdlc_task`` table."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng, tables=[SDLCTask.__table__])
    return eng


@pytest.fixture
def patched_session(engine):
    """Point ``database_service.run_in_session`` at a real SQLite session."""

    def _fake(block, op=None, **kwargs):
        with Session(engine) as session:
            return block(session)

    with patch.object(database_service, "run_in_session", side_effect=_fake):
        yield engine


def _add(engine, task_id: str, status: SDLCTaskStatus, age_minutes: int) -> None:
    when = datetime.now(UTC) - timedelta(minutes=age_minutes)
    with Session(engine) as session:
        session.add(
            SDLCTask(
                id=task_id,
                thread_id=f"th-{task_id}",
                status=status,
                requirements="r",
                plan="[]",
                created_at=when,
                updated_at=when,
            )
        )
        session.commit()


def _status(engine, task_id: str):
    with Session(engine) as session:
        return session.get(SDLCTask, task_id).status


class TestStaleTaskReaper:
    """Tests for the stale SDLC task reaper."""

    def test_reaps_only_stale_non_terminal_tasks(self, patched_session) -> None:
        """Only stale tasks in a non-terminal state are reaped."""
        engine = patched_session
        _add(engine, "stale-planning", SDLCTaskStatus.PLANNING, age_minutes=180)
        _add(engine, "stale-reviewing", SDLCTaskStatus.REVIEWING, age_minutes=90)
        _add(engine, "fresh-coding", SDLCTaskStatus.CODING, age_minutes=5)
        _add(engine, "done", SDLCTaskStatus.COMPLETED, age_minutes=180)
        _add(engine, "cancelled", SDLCTaskStatus.CANCELLED, age_minutes=180)
        _add(engine, "paused", SDLCTaskStatus.PAUSED, age_minutes=180)

        service = SDLCTaskService()
        reaped = service._reap_stale_tasks_sync(timeout_minutes=60)

        assert reaped == 2, "the status filter must actually match the stored rows"
        assert _status(engine, "stale-planning") == SDLCTaskStatus.FAILED
        assert _status(engine, "stale-reviewing") == SDLCTaskStatus.FAILED
        # Untouched:
        assert _status(engine, "fresh-coding") == SDLCTaskStatus.CODING
        assert _status(engine, "done") == SDLCTaskStatus.COMPLETED
        assert _status(engine, "cancelled") == SDLCTaskStatus.CANCELLED
        # PAUSED is a deliberate user state, not an interrupted run.
        assert _status(engine, "paused") == SDLCTaskStatus.PAUSED

    def test_reap_is_idempotent(self, patched_session) -> None:
        """Reaping twice leaves the same result as reaping once."""
        engine = patched_session
        _add(engine, "stale", SDLCTaskStatus.PLANNING, age_minutes=180)

        service = SDLCTaskService()
        assert service._reap_stale_tasks_sync(timeout_minutes=60) == 1
        # Second pass finds nothing: the row is now terminal.
        assert service._reap_stale_tasks_sync(timeout_minutes=60) == 0

    def test_nothing_reaped_when_all_fresh(self, patched_session) -> None:
        """Fresh tasks are left untouched."""
        engine = patched_session
        _add(engine, "a", SDLCTaskStatus.PLANNING, age_minutes=1)
        _add(engine, "b", SDLCTaskStatus.CODING, age_minutes=30)

        service = SDLCTaskService()
        assert service._reap_stale_tasks_sync(timeout_minutes=60) == 0

    def test_all_active_statuses_are_reapable(self, patched_session) -> None:
        """Every non-terminal, non-paused status must be covered."""
        engine = patched_session
        expected = {
            SDLCTaskStatus.SPECING,
            SDLCTaskStatus.PLANNING,
            SDLCTaskStatus.CODING,
            SDLCTaskStatus.TESTING,
            SDLCTaskStatus.REVIEWING,
        }
        assert set(SDLCTaskService.ACTIVE_STATUSES) == expected

        for i, status in enumerate(sorted(expected, key=lambda s: s.name)):
            _add(engine, f"t{i}", status, age_minutes=180)

        service = SDLCTaskService()
        assert service._reap_stale_tasks_sync(timeout_minutes=60) == len(expected)

    async def test_zero_timeout_disables_reaping(self, patched_session) -> None:
        """A zero timeout disables reaping entirely."""
        engine = patched_session
        _add(engine, "stale", SDLCTaskStatus.PLANNING, age_minutes=180)

        service = SDLCTaskService()
        assert await service.reap_stale_tasks(timeout_minutes=0) == 0
        assert _status(engine, "stale") == SDLCTaskStatus.PLANNING

    async def test_failure_is_swallowed(self) -> None:
        """A reaper failure must never break startup."""
        service = SDLCTaskService()
        with patch.object(
            SDLCTaskService,
            "_reap_stale_tasks_sync",
            side_effect=RuntimeError("db down"),
        ):
            assert await service.reap_stale_tasks(timeout_minutes=60) == 0


class TestCoderModelPersistence:
    """``sdlc_task.coder_model`` backs the 「模型」 column in the task list."""

    def test_create_task_persists_coder_model(self, patched_session) -> None:
        """create_task() persists the selected coder model."""
        engine = patched_session
        service = SDLCTaskService()

        service._create_task_sync(
            task_id="t1",
            thread_id="th-1",
            user_id=1,
            requirements="do a thing",
            coder_model="deepseek-v4-pro",
        )

        with Session(engine) as session:
            assert session.get(SDLCTask, "t1").coder_model == "deepseek-v4-pro"

    def test_create_task_without_model_leaves_it_null(self, patched_session) -> None:
        """create_task() leaves the coder model null when none is given."""
        engine = patched_session
        service = SDLCTaskService()

        service._create_task_sync(
            task_id="t2",
            thread_id="th-2",
            user_id=1,
            requirements="do a thing",
        )

        with Session(engine) as session:
            assert session.get(SDLCTask, "t2").coder_model is None
