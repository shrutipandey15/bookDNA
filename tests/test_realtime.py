"""Realtime transport [phases 1–3].

Covers the parts that don't need a live Redis or a real socket:
  - publish is a no-op (never raises) when Redis is unavailable
  - the WS auth handshake accepts a valid access token and rejects the rest
  - notify() emits a contentless realtime envelope alongside the DB write
  - scope strings parse safely and authorization is membership/party gated

End-to-end socket + presence + typing delivery is exercised by the frontend
suite and by hand.
"""

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


# ── publish degrades quietly without Redis ──

async def test_publish_is_silent_noop_without_redis(monkeypatch):
    from app.services import realtime_service

    async def _no_redis():
        return None

    monkeypatch.setattr(realtime_service, "get_client", _no_redis)
    # Must not raise — callers treat it as a courtesy layer.
    await realtime_service.publish("00000000-0000-0000-0000-000000000000", {"type": "notify", "kind": "x"})


async def test_subscribe_yields_idle_ticks_without_redis(monkeypatch):
    from app.services import realtime_service

    async def _no_redis():
        return None

    monkeypatch.setattr(realtime_service, "get_client", _no_redis)
    monkeypatch.setattr(realtime_service, "HEARTBEAT_SECONDS", 0)

    gen = realtime_service.subscribe("u1")
    first = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert first is None
    await gen.aclose()


# ── WS auth handshake ──

class _FakeWS:
    def __init__(self, incoming):
        self._incoming = list(incoming)

    async def receive_json(self):
        if not self._incoming:
            raise asyncio.TimeoutError
        return self._incoming.pop(0)


async def test_authenticate_accepts_valid_access_token():
    from app.routers.realtime import _authenticate
    from app.services.auth_service import create_access_token
    import uuid

    uid = uuid.uuid4()
    token = create_access_token(uid)
    got = await _authenticate(_FakeWS([{"type": "auth", "token": token}]))
    assert got == uid


@pytest.mark.parametrize("frame", [
    {"type": "auth", "token": "not-a-jwt"},
    {"type": "auth"},
    {"type": "hello", "token": "x"},
    "a string, not an object",
])
async def test_authenticate_rejects_bad_frames(frame):
    from app.routers.realtime import _authenticate
    assert await _authenticate(_FakeWS([frame])) is None


async def test_authenticate_rejects_a_refresh_token():
    from app.routers.realtime import _authenticate
    from app.services.auth_service import create_refresh_token_str
    import uuid

    token, _ = create_refresh_token_str(uuid.uuid4())
    assert await _authenticate(_FakeWS([{"type": "auth", "token": token}])) is None


# ── Scope parsing + authorization [phases 2–3] ──

@pytest.mark.parametrize("bad", [
    "", "collection", "collection:", "collection:not-a-uuid", "room:" + "0" * 32,
    "thread:" + "x", 123, None, "collection:" + "a" * 200,
])
def test_parse_scope_rejects_junk(bad):
    from app.services.realtime_scope import parse_scope
    assert parse_scope(bad) is None


def test_parse_scope_accepts_the_two_kinds():
    import uuid
    from app.services.realtime_scope import parse_scope

    cid = uuid.uuid4()
    assert parse_scope(f"collection:{cid}") == ("collection", cid)
    tid = uuid.uuid4()
    assert parse_scope(f"thread:{tid}") == ("thread", tid)


async def test_authorize_collection_scope_is_membership_gated(client):
    import uuid
    from app.services.realtime_scope import authorize

    async def _reg(name):
        await client.post("/api/auth/register", json={
            "email": f"{name}@example.com", "username": f"usr_{name}", "password": "hunter2pass",
        })
        r = await client.post("/api/auth/login", json={
            "email": f"{name}@example.com", "password": "hunter2pass",
        })
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        uid = (await client.get("/api/auth/me", headers=h)).json()["id"]
        return h, uuid.UUID(uid)

    owner_h, owner_id = await _reg("scpOwner")
    out_h, out_id = await _reg("scpOut")
    cid = (await client.post("/api/collections", json={"title": "Room"}, headers=owner_h)).json()["id"]

    assert await authorize(f"collection:{cid}", owner_id) is True
    assert await authorize(f"collection:{cid}", out_id) is False
    assert await authorize(f"collection:{uuid.uuid4()}", owner_id) is False


async def test_authorize_rejects_bogus_thread_and_malformed_scope():
    import uuid
    from app.services.realtime_scope import authorize

    assert await authorize(f"thread:{uuid.uuid4()}", uuid.uuid4()) is False
    assert await authorize("nonsense", uuid.uuid4()) is False


async def test_notify_publishes_contentless_realtime_event(client, monkeypatch):
    calls = []

    async def _capture(user_id, event):
        calls.append((str(user_id), event))

    from app.services import notification_service
    monkeypatch.setattr(notification_service, "realtime_publish", _capture)

    async def _reg(name):
        await client.post("/api/auth/register", json={
            "email": f"{name}@example.com", "username": f"usr_{name}", "password": "hunter2pass",
        })
        r = await client.post("/api/auth/login", json={
            "email": f"{name}@example.com", "password": "hunter2pass",
        })
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    a = await _reg("rtA")
    b = await _reg("rtB")
    echo = (await client.post("/api/echoes", json={
        "body": "a real reflection here", "book_title": "Piranesi", "primary_emotion": "awe",
    }, headers=a)).json()["echo"]["id"]

    await client.post(f"/api/echoes/{echo}/replies", json={"body": "felt the same"}, headers=b)

    assert calls, "notify() did not publish a realtime event"
    _, event = calls[-1]
    assert event == {"type": "notify", "kind": "echo_reply"}
    # Contentless: no book, no handle, no ids.
    assert set(event) == {"type", "kind"}
