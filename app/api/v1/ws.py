"""WebSocket endpoint for real-time SDLC interaction.

Provides bidirectional communication for:
- Server push: graph status changes, agent events, interrupt notifications
- Client commands: approve, reject, status query

Does NOT support cancel or modify (LangGraph astream_events cannot be
cancelled externally; mid-execution state modification risks inconsistency).

Enabled via ENABLE_WEBSOCKET=true Feature Flag (default false).
SSE endpoints (/sdlc/stream) remain the primary streaming interface.
"""

import json
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.langgraph.workflows import SupervisorGraph
from app.core.logging import logger
from app.models.session import Session
from app.services.database import database_service as db_service
from app.utils.auth import verify_token

router = APIRouter()
orchestrator = SupervisorGraph()


class ConnectionManager:
    """Manage active WebSocket connections per thread_id.

    One thread_id can have at most one active connection to prevent
    concurrent state mutations on the same graph.
    """

    def __init__(self) -> None:
        """Initialize an empty connection registry."""
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, thread_id: str, websocket: WebSocket) -> bool:
        """Accept a connection for a thread_id. Returns False if conflict."""
        if thread_id in self._connections:
            return False
        await websocket.accept()
        self._connections[thread_id] = websocket
        return True

    def disconnect(self, thread_id: str) -> None:
        """Remove a connection."""
        self._connections.pop(thread_id, None)

    async def send_json(self, thread_id: str, data: dict[str, Any]) -> None:
        """Send a JSON message to the connection for a thread_id."""
        ws = self._connections.get(thread_id)
        if ws:
            await ws.send_json(data)


manager = ConnectionManager()


async def _authenticate_ws(token: str) -> Session | None:
    """Validate JWT token and return the authenticated session.

    Reuses ``verify_token`` from app.utils.auth for parity with the HTTP
    ``get_current_session`` dependency. The token's ``sub`` claim must be
    a valid session_id (tokens are issued by POST /auth/session).

    WebSocket cannot rely on the standard Authorization header across
    browsers, so the token is passed as a query parameter.
    """
    try:
        session_id = verify_token(token)
        if not session_id:
            return None

        session = await db_service.get_session(session_id)
        if not session:
            return None

        return session
    except ValueError:
        logger.warning("ws_auth_failed", reason="invalid_token_format")
        return None


async def _handle_client_command(
    thread_id: str,
    command: str,
    payload: dict[str, Any],
    session: Session,
) -> dict[str, Any]:
    """Process a client command and return a response dict.

    Supported commands:
    - approve: Resume an interrupted graph with approval
    - reject: Resume an interrupted graph with rejection
    - status: Query current graph state (non-mutating)
    """
    if command == "approve":
        logger.info("ws_approve_received", thread_id=thread_id, session_id=session.id)
        response = await orchestrator.approve_task(session_id=thread_id)
        return {
            "type": "command_result",
            "command": "approve",
            "status": response.get("status", "unknown"),
            "is_complete": response.get("is_complete", False),
            "human_approval_needed": response.get("human_approval_needed", False),
        }

    if command == "reject":
        reason = payload.get("reason", "")
        logger.info(
            "ws_reject_received",
            thread_id=thread_id,
            session_id=session.id,
            reason=reason,
        )
        response = await orchestrator.reject_task(session_id=thread_id, reason=reason)
        return {
            "type": "command_result",
            "command": "reject",
            "status": response.get("status", "unknown"),
            "is_complete": response.get("is_complete", False),
        }

    if command == "status":
        return {
            "type": "status",
            "thread_id": thread_id,
            "connected": True,
        }

    return {
        "type": "error",
        "message": f"Unknown command: {command}. Supported: approve, reject, status",
    }


@router.websocket("/ws/sdlc/{thread_id}")
async def sdlc_websocket(
    websocket: WebSocket,
    thread_id: str,
    token: str = Query(..., description="JWT token for authentication"),
) -> None:
    """WebSocket endpoint for real-time SDLC interaction.

    Client sends JSON messages: {"command": "approve|reject|status", "payload": {...}}
    Server sends JSON events: {"type": "command_result|status|error", ...}

    Authentication via ?token=<JWT> query parameter.
    One connection per thread_id (concurrent connections rejected).
    """
    if not settings.ENABLE_WEBSOCKET:
        await websocket.close(code=4003, reason="WebSocket disabled (ENABLE_WEBSOCKET=false)")
        return

    session = await _authenticate_ws(token)
    if not session:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    connected = await manager.connect(thread_id, websocket)
    if not connected:
        await websocket.close(code=4002, reason="Thread already has an active connection")
        return

    logger.info("ws_connected", thread_id=thread_id, session_id=session.id)

    try:
        await manager.send_json(
            thread_id,
            {
                "type": "connected",
                "thread_id": thread_id,
                "message": "WebSocket connected. Send commands: approve, reject, status",
            },
        )

        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_json(
                    thread_id,
                    {"type": "error", "message": "Invalid JSON received"},
                )
                continue

            command = data.get("command", "")
            payload = data.get("payload", {})

            if command not in ("approve", "reject", "status"):
                await manager.send_json(
                    thread_id,
                    {
                        "type": "error",
                        "message": f"Unsupported command: {command}. Supported: approve, reject, status",
                    },
                )
                continue

            try:
                result = await _handle_client_command(thread_id, command, payload, session)
                await manager.send_json(thread_id, result)
            except Exception as e:
                logger.exception("ws_command_failed", thread_id=thread_id, command=command)
                await manager.send_json(
                    thread_id,
                    {"type": "error", "command": command, "message": str(e)},
                )

    except WebSocketDisconnect:
        logger.info("ws_disconnected", thread_id=thread_id)
    except Exception as e:
        logger.exception("ws_error", thread_id=thread_id, error=str(e))
    finally:
        manager.disconnect(thread_id)
