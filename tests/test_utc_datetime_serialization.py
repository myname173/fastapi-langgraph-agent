"""Regression tests for UTC-aware datetime serialisation.

Context: every timestamp column in this schema is ``TIMESTAMP WITHOUT TIME
ZONE``, so a value written as ``datetime.now(UTC)`` is read back **naive**.
Pydantic then rendered it as ``2026-10-06T09:32:38.940196`` with no offset, and
browsers parse a naive ISO date-time as *local* time (ECMAScript spec). Every
timestamp in the UI was therefore shifted by the viewer's UTC offset — a
two-minute-old SDLC task displayed as "8 小时前" for a GMT+8 viewer.

``UTCDateTimeMixin`` makes the wire format unambiguous by emitting an explicit
UTC offset, so the browser renders it in local time itself.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.models.alert import Alert
from app.models.approval_log import ApprovalLog
from app.models.base import UTCDateTimeMixin, utc_isoformat
from app.models.goal import Goal, GoalStatus
from app.models.llm_execution import LLMExecution
from app.models.media_history import MediaHistory
from app.models.portfolio import Holding, Portfolio
from app.models.risk_profile import RiskProfile
from app.models.sdlc_task import SDLCTask
from app.models.thread import Thread
from app.models.watchlist import WatchlistItem
from app.schemas.auth import SessionResponse, Token
from app.schemas.goal import GoalResponse

# A naive value as it comes back from a `TIMESTAMP WITHOUT TIME ZONE` column
# after the app wrote `datetime.now(UTC)`.
NAIVE_UTC = datetime(2026, 10, 6, 9, 32, 38, 940196)
EXPECTED = "2026-10-06T09:32:38.940196Z"

ALL_TABLE_MODELS = [
    Alert,
    ApprovalLog,
    Goal,
    LLMExecution,
    MediaHistory,
    Portfolio,
    Holding,
    RiskProfile,
    SDLCTask,
    Thread,
    WatchlistItem,
]

ALL_RESPONSE_SCHEMAS = [GoalResponse, Token, SessionResponse]


class TestUtcIsoformat:
    """The helper itself."""

    def test_naive_is_assumed_utc(self) -> None:
        """A naive value is treated as UTC, not local time."""
        assert utc_isoformat(NAIVE_UTC) == EXPECTED

    def test_aware_utc_is_unchanged(self) -> None:
        """An already-aware UTC value serialises identically."""
        assert utc_isoformat(NAIVE_UTC.replace(tzinfo=UTC)) == EXPECTED

    def test_non_utc_offset_is_converted(self) -> None:
        """A +08:00 value is converted to the same instant in UTC."""
        local = NAIVE_UTC.replace(tzinfo=timezone(timedelta(hours=8)))
        # 09:32:38+08:00 == 01:32:38Z
        assert utc_isoformat(local) == "2026-10-06T01:32:38.940196Z"

    def test_none_passes_through(self) -> None:
        """Optional timestamps stay null."""
        assert utc_isoformat(None) is None


class TestTableModelsEmitOffset:
    """Every ORM table serialises its datetimes with an explicit offset."""

    @pytest.mark.parametrize("model", ALL_TABLE_MODELS, ids=lambda m: m.__name__)
    def test_datetime_columns_serialise_with_z(self, model: type) -> None:
        """Each datetime column of each table ends in ``Z``.

        ``model_construct`` skips validation so we don't have to satisfy every
        required field — the serialiser is what we're testing, and it still runs.
        """
        dt_fields = [c.name for c in model.__table__.c if str(c.type) == "DATETIME"]
        assert dt_fields, f"{model.__name__} declares no datetime column"

        payload = model.model_construct(**{name: NAIVE_UTC for name in dt_fields}).model_dump(mode="json")

        for name in dt_fields:
            assert payload[name] == EXPECTED, f"{model.__name__}.{name} lost its UTC offset"

    @pytest.mark.parametrize("model", ALL_TABLE_MODELS, ids=lambda m: m.__name__)
    def test_column_types_are_unchanged(self, model: type) -> None:
        """Guard against accidental schema drift.

        The fix is serialisation-only: the columns must stay plain
        ``DATETIME`` (i.e. ``timestamp without time zone``) so no migration is
        required and existing rows keep their meaning.
        """
        for column in model.__table__.c:
            if column.name in {"created_at", "updated_at", "completed_at", "fired_at", "timestamp"}:
                assert str(column.type) == "DATETIME", f"{model.__name__}.{column.name} changed type"


class TestResponseSchemasEmitOffset:
    """Schemas re-serialise datetimes, so they need the mixin too."""

    @pytest.mark.parametrize("schema", ALL_RESPONSE_SCHEMAS, ids=lambda s: s.__name__)
    def test_schema_inherits_mixin(self, schema: type) -> None:
        """Response schemas carry the mixin so a model→schema copy keeps the offset."""
        assert issubclass(schema, UTCDateTimeMixin)

    def test_goal_response_serialises_with_z(self) -> None:
        """A goal response built from naive ORM values still emits ``Z``."""
        payload = GoalResponse.model_construct(
            id="g1",
            user_id=1,
            title="t",
            description="d",
            status=GoalStatus.ACTIVE,
            created_at=NAIVE_UTC,
            updated_at=NAIVE_UTC,
            completed_at=None,
        ).model_dump(mode="json")
        assert payload["created_at"] == EXPECTED
        assert payload["updated_at"] == EXPECTED
        assert payload["completed_at"] is None


class TestFastApiResponse:
    """End-to-end: the offset survives FastAPI's own serialisation path."""

    def test_response_model_emits_offset(self) -> None:
        """A ``response_model`` endpoint must not strip the offset.

        This is the assertion that actually reproduces the production bug:
        FastAPI serialises Pydantic response models itself (it does not go
        through ``fastapi.encoders.ENCODERS_BY_TYPE``), so an encoder-level
        override is not enough — the mixin has to be on the model.
        """

        class Item(UTCDateTimeMixin, BaseModel):
            created_at: datetime

        app = FastAPI()

        @app.get("/item", response_model=Item)
        def get_item() -> Item:
            return Item(created_at=NAIVE_UTC)

        body = TestClient(app).get("/item").json()
        assert body["created_at"] == EXPECTED

    def test_browser_parses_offset_as_utc(self) -> None:
        """Sanity-check the contract the frontend relies on.

        The frontend does ``new Date(ts)``; an offset-bearing string parses to
        the correct instant, whereas the naive form would be read as local time.
        """
        aware = datetime.fromisoformat(EXPECTED.replace("Z", "+00:00"))
        assert aware == NAIVE_UTC.replace(tzinfo=UTC)
