"""Watchlist REST endpoints for the chat stock-advisor."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.v1.auth import get_current_session
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.session import Session
from app.services.watchlist_service import watchlist_service
from app.utils.errors import safe_500

router = APIRouter()


@router.get("")
@limiter.limit("30 per minute")
async def list_watchlist(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Return all watchlist items for the current user."""
    try:
        items = await watchlist_service.list(user_id=session.user_id)
        return [
            {
                "id": i.id,
                "symbol": i.symbol,
                "name": i.name,
                "notes": i.notes,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in items
        ]
    except Exception as e:
        logger.error("watchlist_list_failed", error=str(e))
        raise safe_500(e, context="watchlist")


@router.post("")
@limiter.limit("20 per minute")
async def add_to_watchlist(
    request: Request,
    symbol: str = Query(...),
    name: str = Query(""),
    notes: str = Query(""),
    session: Session = Depends(get_current_session),
):
    """Add a symbol to the current user's watchlist."""
    try:
        item = await watchlist_service.add(user_id=session.user_id, symbol=symbol, name=name, notes=notes)
        return {
            "id": item.id,
            "symbol": item.symbol,
            "name": item.name,
            "notes": item.notes,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
    except Exception as e:
        logger.error("watchlist_add_failed", error=str(e))
        raise safe_500(e, context="watchlist")


@router.delete("/{item_id}")
@limiter.limit("20 per minute")
async def remove_from_watchlist(
    item_id: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Remove a watchlist item owned by the current user."""
    try:
        ok = await watchlist_service.remove(item_id=item_id, user_id=session.user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("watchlist_remove_failed", error=str(e))
        raise safe_500(e, context="watchlist")
