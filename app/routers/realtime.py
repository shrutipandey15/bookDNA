"""WebSocket endpoint for in-app realtime. [realtime phases 1–3]

A browser can't set ``Authorization`` on a WebSocket handshake, so auth is the
first frame the client sends:

    {"type": "auth", "token": "<access JWT>"}

within 5 seconds. After that the socket is bound to that user.

Server → client frames:
    {"type": "ready"}                                    handshake ok
    {"type": "ping"}                                     heartbeat, ~every 15s
    {"type": "notify", "kind": "..."}                    a notification landed (phase 1)
    {"type": "presence_roster", "scope", "present": []}  who's in a scope you just entered
    {"type": "presence", "scope", "user", "present"}     someone entered/left a scope
    {"type": "typing", "scope", "user"}                  someone is typing in a scope

Client → server frames (after auth):
    {"type": "scope_enter", "scope": "collection:<uuid>" | "thread:<uuid>"}
    {"type": "scope_leave", "scope": "..."}
    {"type": "typing", "scope": "..."}

A scope is authorized once, with a fresh short-lived DB session, on
``scope_enter``. No session is held for the life of the socket.

Transport shape: the socket subscribes ONCE to ``rt:user:{uid}`` and pattern
``rt:scope:*`` and never changes its subscriptions after that — ``pump_out`` is
the only task that touches the pubsub. Scope traffic (presence, typing) is
low-volume and filtered locally against the set of scopes this socket has
actually entered. That trades a little redundant fan-out for not having to
serialise dynamic (un)subscribes against a concurrent read.
"""

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from starlette.websockets import WebSocketState

from app.database import async_session
from app.models.user import User
from app.services import realtime_service as rt
from app.services.auth_service import decode_token
from app.services.realtime_scope import authorize as authorize_scope

logger = logging.getLogger("bibliome.realtime")

router = APIRouter(prefix="/realtime", tags=["realtime"])

_AUTH_TIMEOUT = 5.0
_TYPING_MIN_INTERVAL = 1.0  # server-side throttle per scope, seconds
_MAX_SCOPES = 20            # a client should hold one or two; this is just a ceiling


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


async def _load_handle(user_id: uuid.UUID) -> str | None:
    try:
        async with async_session() as db:
            return (
                await db.execute(select(User.handle).where(User.id == user_id))
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
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

    handle = await _load_handle(user_id)
    await ws.send_json({"type": "ready"})

    client = await rt.get_client()
    if client is None or not handle:
        # Degraded: no Redis (or no handle to name in presence events). Keep the
        # socket alive so the client doesn't reconnect-storm; deliver nothing.
        await _run_degraded(ws)
        return

    pubsub = client.pubsub()
    await pubsub.subscribe(rt.user_channel(user_id))
    await pubsub.psubscribe("rt:scope:*")

    scopes: set[str] = set()
    last_typing: dict[str, float] = {}

    async def announce_leave(scope: str):
        await rt.presence_leave(scope, handle)
        await rt.publish_scope(
            scope, {"type": "presence", "scope": scope, "user": handle, "present": False}
        )

    async def handle_frame(frame: dict):
        kind = frame.get("type")
        scope = frame.get("scope")
        if not isinstance(scope, str):
            return

        if kind == "scope_enter":
            if scope in scopes or len(scopes) >= _MAX_SCOPES:
                return
            if not await authorize_scope(scope, user_id):
                return
            scopes.add(scope)
            await rt.presence_join(scope, handle)
            roster = [h for h in await rt.presence_roster(scope) if h != handle]
            await ws.send_json({"type": "presence_roster", "scope": scope, "present": roster})
            await rt.publish_scope(
                scope, {"type": "presence", "scope": scope, "user": handle, "present": True}
            )

        elif kind == "scope_leave":
            if scope in scopes:
                scopes.discard(scope)
                await announce_leave(scope)

        elif kind == "typing":
            if scope not in scopes:
                return
            now = time.monotonic()
            if now - last_typing.get(scope, 0.0) < _TYPING_MIN_INTERVAL:
                return
            last_typing[scope] = now
            await rt.publish_scope(scope, {"type": "typing", "scope": scope, "user": handle})

    async def pump_out():
        idle_since = time.monotonic()
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                if time.monotonic() - idle_since >= rt.HEARTBEAT_SECONDS:
                    idle_since = time.monotonic()
                    for scope in list(scopes):
                        await rt.presence_touch(scope, handle)
                    await ws.send_json({"type": "ping"})
                continue
            idle_since = time.monotonic()
            try:
                event = json.loads(msg["data"])
            except (ValueError, TypeError):
                continue
            # Pattern channel: only forward scopes this socket actually entered.
            ev_scope = event.get("scope")
            if ev_scope is not None and ev_scope not in scopes:
                continue
            # Don't reflect my own presence/typing back to me.
            if event.get("type") in ("presence", "typing") and event.get("user") == handle:
                continue
            await ws.send_json(event)

    async def pump_in():
        while True:
            raw = await ws.receive_text()  # raises WebSocketDisconnect on close
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(frame, dict):
                await handle_frame(frame)

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    try:
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (out_task, in_task):
            t.cancel()
        await asyncio.gather(out_task, in_task, return_exceptions=True)
        for scope in list(scopes):
            try:
                await announce_leave(scope)
            except Exception:  # noqa: BLE001
                pass
        try:
            aclose = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if aclose is not None:
                res = aclose()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:  # noqa: BLE001
            pass
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass


async def _run_degraded(ws: WebSocket):
    try:
        while True:
            recv = asyncio.create_task(ws.receive_text())
            done, _ = await asyncio.wait({recv}, timeout=rt.HEARTBEAT_SECONDS)
            if recv in done:
                recv.result()  # raises WebSocketDisconnect on close; else ignore frame
            else:
                recv.cancel()
                await ws.send_json({"type": "ping"})
    except Exception:  # noqa: BLE001 — disconnect or otherwise, we're done
        pass
    finally:
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
