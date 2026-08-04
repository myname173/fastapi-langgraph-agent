"""Alert / price-breakout REST endpoints for the chat stock-advisor."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.api.v1.auth import get_current_session
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.alert import AlertCondition
from app.models.session import Session
from app.services.alert_service import alert_service
from app.utils.errors import safe_500

router = APIRouter()


class CreateAlertBody(BaseModel):
    """Request body for creating a price-breakout alert."""

    symbol: str
    name: str = ""
    condition: AlertCondition = AlertCondition.ABOVE
    price: float
    note: str = ""


class UpdateAlertBody(BaseModel):
    """Request body for patching mutable alert fields."""

    active: bool | None = None
    price: float | None = None
    condition: AlertCondition | None = None
    note: str | None = None


def _serialize(a):
    return {
        "id": a.id,
        "symbol": a.symbol,
        "name": a.name,
        "condition": a.condition.value,
        "price": a.price,
        "active": a.active,
        "fired_at": a.fired_at.isoformat() if a.fired_at else None,
        "note": a.note,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


@router.get("")
@limiter.limit("30 per minute")
async def list_alerts(
    request: Request,
    active_only: bool = Query(default=False),
    session: Session = Depends(get_current_session),
):
    """Return all alerts for the current user, optionally active-only."""
    try:
        alerts = await alert_service.list(user_id=session.user_id, active_only=active_only)
        return [_serialize(a) for a in alerts]
    except Exception as e:
        logger.error("alert_list_failed", error=str(e))
        raise safe_500(e, context="alert")


@router.post("")
@limiter.limit("15 per minute")
async def create_alert(
    request: Request,
    body: CreateAlertBody,
    session: Session = Depends(get_current_session),
):
    """Create a new price-breakout alert for the current user."""
    try:
        a = await alert_service.create(
            user_id=session.user_id,
            symbol=body.symbol,
            name=body.name,
            condition=body.condition,
            price=body.price,
            note=body.note,
        )
        return _serialize(a)
    except Exception as e:
        logger.error("alert_create_failed", error=str(e))
        raise safe_500(e, context="alert")


@router.patch("/{alert_id}")
@limiter.limit("15 per minute")
async def update_alert(
    alert_id: str,
    request: Request,
    body: UpdateAlertBody,
    session: Session = Depends(get_current_session),
):
    """Patch mutable fields of an existing alert owned by the user."""
    try:
        a = await alert_service.update(
            alert_id=alert_id,
            user_id=session.user_id,
            active=body.active,
            price=body.price,
            condition=body.condition,
            note=body.note,
        )
        if not a:
            raise HTTPException(status_code=404, detail="not found")
        return _serialize(a)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("alert_update_failed", error=str(e))
        raise safe_500(e, context="alert")


@router.post("/{alert_id}/fire")
@limiter.limit("10 per minute")
async def fire_alert(
    alert_id: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Mark an active alert as fired (one-shot disable)."""
    try:
        a = await alert_service.fire(alert_id=alert_id, user_id=session.user_id)
        if not a:
            raise HTTPException(status_code=404, detail="not found or already fired")
        return _serialize(a)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("alert_fire_failed", error=str(e))
        raise safe_500(e, context="alert")


@router.delete("/{alert_id}")
@limiter.limit("20 per minute")
async def delete_alert(
    alert_id: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Delete an alert owned by the current user."""
    try:
        ok = await alert_service.delete(alert_id=alert_id, user_id=session.user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("alert_delete_failed", error=str(e))
        raise safe_500(e, context="alert")
