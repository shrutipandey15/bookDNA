"""In-app realtime transport. [realtime phases 1–3]

A thin pub/sub layer over Redis so events created in one uvicorn worker reach a
WebSocket held open by another (the service runs 2 workers). Every publish is
best-effort and never raises into its caller — the same contract as
``push_service``.

Redis is REQUIRED for cross-worker delivery. Without ``REDIS_URL`` the transport
is inert: publishes are no-ops, the socket carries nothing, and every frontend
surface keeps its polling fallback.

Channels
--------
* ``rt:user:{uuid}``     — one per user. Notifications and message nudges.
* ``rt:scope:{scope}``   — one per open conversation. A *scope* is
  ``collection:{uuid}`` or ``thread:{uuid}``. Presence and typing ride here.

Presence
--------
``rt:present:{scope}`` is a Redis hash, field = handle, value = last-seen epoch.
Entries older than ``PRESENCE_TTL`` are treated as gone (a crashed worker can't
clean up after itself; the timestamp check does). The whole key also carries a
short EXPIRE so an abandoned scope disappears on its own.

Payloads are small JSON envelopes; presence/typing name a bare handle, which
co-participants can already see, and nothing else.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import redis.asyncio as redis

from app.config import get_settings

logger = logging.getLogger("bibliome.realtime")

_client: redis.Redis | None = None
_lock = asyncio.Lock()

HEARTBEAT_SECONDS = 15
PRESENCE_TTL = 45          # a presence entry older than this is stale
_PRESENCE_KEY_EXPIRE = 120  # abandoned-scope cleanup


def user_channel(user_id: uuid.UUID | str) -> str:
    return f"rt:user:{user_id}"


def scope_channel(scope: str) -> str:
    return f"rt:scope:{scope}"


def _present_key(scope: str) -> str:
    return f"rt:present:{scope}"


async def get_client() -> redis.Redis | None:
    """Dedicated client for realtime. Separate from the rate-limit one, whose 2s
    socket_timeout would abort a blocking SUBSCRIBE read."""
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client
        url = getattr(get_settings(), "REDIS_URL", None)
        if not url:
            logger.info(
                "No REDIS_URL — realtime transport disabled, clients fall back to polling"
            )
            return None
        try:
            client = redis.from_url(url, decode_responses=True, health_check_interval=30)
            await client.ping()
            _client = client
            logger.info("Redis connected for realtime transport")
            return _client
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Redis unavailable for realtime (%s) — clients fall back to polling", e
            )
            return None


async def _publish(channel: str, event: dict) -> None:
    try:
        client = await get_client()
        if client is None:
            return
        await client.publish(channel, json.dumps(event))
    except Exception:  # noqa: BLE001 — a courtesy layer cannot break the caller
        logger.exception("realtime publish failed on %s", channel)


async def publish_user(user_id: uuid.UUID | str, event: dict) -> None:
    """Fan an event out to every socket this user has open."""
    await _publish(user_channel(user_id), event)


# Back-compat name used by notification_service.
publish = publish_user


async def publish_scope(scope: str, event: dict) -> None:
    """Fan an event out to everyone with `scope` open. The sender receives it
    too; sockets suppress their own presence/typing echo by handle."""
    await _publish(scope_channel(scope), event)


# ── Presence ──

async def presence_join(scope: str, handle: str) -> None:
    try:
        client = await get_client()
        if client is None:
            return
        key = _present_key(scope)
        await client.hset(key, handle, str(int(time.time())))
        await client.expire(key, _PRESENCE_KEY_EXPIRE)
    except Exception:  # noqa: BLE001
        logger.exception("presence_join failed for %s in %s", handle, scope)


async def presence_touch(scope: str, handle: str) -> None:
    await presence_join(scope, handle)  # same write, refreshes the timestamp


async def presence_leave(scope: str, handle: str) -> None:
    try:
        client = await get_client()
        if client is None:
            return
        await client.hdel(_present_key(scope), handle)
    except Exception:  # noqa: BLE001
        logger.exception("presence_leave failed for %s in %s", handle, scope)


async def presence_roster(scope: str) -> list[str]:
    """Handles currently present in `scope`, stale entries filtered out."""
    try:
        client = await get_client()
        if client is None:
            return []
        raw = await client.hgetall(_present_key(scope))
    except Exception:  # noqa: BLE001
        logger.exception("presence_roster failed for %s", scope)
        return []
    cutoff = int(time.time()) - PRESENCE_TTL
    fresh, stale = [], []
    for handle, seen in raw.items():
        try:
            (fresh if int(seen) >= cutoff else stale).append(handle)
        except (TypeError, ValueError):
            stale.append(handle)
    if stale:
        try:
            await client.hdel(_present_key(scope), *stale)
        except Exception:  # noqa: BLE001
            pass
    return fresh


# ── Subscriber (phase 1 style — a bare user channel, used by tests) ──

async def subscribe(user_id: uuid.UUID | str) -> AsyncIterator[dict | None]:
    """Yield events for this user's channel until the caller stops iterating.
    Yields ``None`` on each idle tick. The WebSocket handler manages its own
    pubsub directly (it needs dynamic scope channels); this stays for tests and
    any simple single-channel consumer."""
    client = await get_client()
    if client is None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            yield None

    pubsub = client.pubsub()
    await pubsub.subscribe(user_channel(user_id))
    try:
        while True:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
            )
            if msg is None:
                yield None
                continue
            try:
                yield json.loads(msg["data"])
            except (ValueError, TypeError):
                logger.warning("realtime: dropped malformed event for %s", user_id)
    finally:
        try:
            await pubsub.unsubscribe(user_channel(user_id))
            aclose = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if aclose is not None:
                res = aclose()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:  # noqa: BLE001
            pass
