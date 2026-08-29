"""WebSocket endpoint for in-app realtime. [realtime phase 1]

A browser can't set ``Authorization`` on a WebSocket handshake, so auth is the
first frame the client sends:

    {"type": "auth", "token": "<access JWT>"}

within 5 seconds. After that the socket is bound to that user and forwards their
realtime channel until it closes. No DB session is held for the life of the
socket — phase 1 needs nothing but the user id from the JWT.

Phase 1 is server→client only. ``pump_in`` just drains frames so close/pong are
processed and a disconnect is noticed promptly; later phases (typing) will read
real messages there.
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.services.auth_service import decode_token
from app.services.realtime_service import subscribe

logger = logging.getLogger("bibliome.realtime")

router = APIRouter(prefix="/realtime", tags=["realtime"])

_AUTH_TIMEOUT = 5.0


async def _authenticate(ws: WebSocket) -> uuid.UUID | None:
    try:
        raw = await asyncio.wait_for(ws.receive_json(), timeout=_AUTH_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("type") != "auth":
        return None
    payload = decode_token(raw.get("token") or "")
    if not payload or payload.get("type") != "access":
        return None
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError, TypeError):
        return None


@router.websocket("/ws")
async def realtime_ws(ws: WebSocket):
    await ws.accept()

    user_id = await _authenticate(ws)
    if user_id is None:
        try:
            await ws.send_json({"type": "auth_error"})
        except Exception:  # noqa: BLE001
            pass
        await ws.close(code=4401)
        return

    await ws.send_json({"type": "ready"})
    events = subscribe(user_id)

    async def pump_out():
        async for event in events:
            # None is an idle tick — send a heartbeat, which also surfaces a
            # broken pipe as an exception that ends this task.
            await ws.send_json(event if event is not None else {"type": "ping"})

    async def pump_in():
        while True:
            await ws.receive_text()  # raises WebSocketDisconnect on close

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    try:
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (out_task, in_task):
            t.cancel()
        await asyncio.gather(out_task, in_task, return_exceptions=True)
        await events.aclose()
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
