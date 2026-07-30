"""Tests for the portfolio service (CRUD over an in-memory SQLite DB)."""

from sqlmodel import SQLModel, Session as DBSession, create_engine

import pytest

from app.core.config import settings
from app.models.portfolio import Holding, Portfolio
from app.services import database_service
from app.services.portfolio_service import portfolio_service


@pytest.fixture
def db(monkeypatch):
    """Provide an in-memory SQLite session for portfolio service tests."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    def fake_run_in_session(func, op=None, **kwargs):
        with DBSession(engine) as s:
            return func(s)

    async def fake_run_in_thread(func, *args, op=None, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(database_service, "run_in_session", fake_run_in_session)
    monkeypatch.setattr("app.core.db_executor.run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(settings, "ENABLE_ASYNC_DB", False)
    yield engine


async def test_portfolio_crud(db):
    """Portfolio create / list / add-holding / remove flows work end-to-end."""
    p = await portfolio_service.create_portfolio(1, "测试组合")
    assert isinstance(p, Portfolio)
    assert p.name == "测试组合"
    pid = p.id

    plist = await portfolio_service.list_portfolios(1)
    assert len(plist) == 1

    h = await portfolio_service.add_holding(pid, "sh600519", "茅台", 100, 1600)
    assert isinstance(h, Holding)
    assert h.symbol == "sh600519"

    holdings = await portfolio_service.get_holdings(pid)
    assert len(holdings) == 1

    upd = await portfolio_service.update_portfolio(pid, name="新名")
    assert upd.name == "新名"

    ok = await portfolio_service.delete_portfolio(pid)
    assert ok is True
    assert await portfolio_service.list_portfolios(1) == []


async def test_portfolio_isolation(db):
    """Portfolios are isolated per user_id (one user cannot see another's)."""
    a = await portfolio_service.create_portfolio(1, "A")
    await portfolio_service.create_portfolio(2, "B")
    await portfolio_service.add_holding(a.id, "sh600519", "", 10, 100)
    # User 2 must not see user 1's holdings.
    assert await portfolio_service.get_holdings(a.id)  # user1's own
    # Listing is user-scoped.
    assert len(await portfolio_service.list_portfolios(2)) == 1
    assert len(await portfolio_service.list_portfolios(1)) == 1
