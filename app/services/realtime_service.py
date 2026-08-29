"""In-app realtime transport. [realtime phase 1 — instant delivery]

A thin pub/sub layer over Redis so an event created in one uvicorn worker
reaches a WebSocket held open by another (the service runs 2 workers). Publish
is best-effort and never raises into its caller — the same contract as
``push_service``.

Redis is REQUIRED for cross-worker delivery. Without ``REDIS_URL`` publish is a
no-op and the socket carries nothing; every frontend surface keeps its polling
fallback, so the app still works, just not instantly.

One channel per user: ``rt:user:{uuid}``. Payloads are small JSON envelopes,
deliberately contentless — the client refetches the affected surface:

    {"type": "notify", "kind": "resonance_message"}
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

import redis.asyncio as redis

from app.config import get_settings

logger = logging.getLogger("bibliome.realtime")

# Dedicated client, separate from the rate-limit one: that client sets a 2s
# socket_timeout, which would abort a blocking SUBSCRIBE read.
_client: redis.Redis | None = None
_lock = asyncio.Lock()

_HEARTBEAT_SECONDS = 15


def _channel(user_id: uuid.UUID | str) -> str:
    return f"rt:user:{user_id}"


async def _redis() -> redis.Redis | None:
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


async def publish(user_id: uuid.UUID | str, event: dict) -> None:
    """Fan an event out to every socket this user has open. Best-effort."""
    try:
        client = await _redis()
        if client is None:
            return
        await client.publish(_channel(user_id), json.dumps(event))
    except Exception:  # noqa: BLE001 — a courtesy layer cannot break the caller
        logger.exception("realtime publish failed for user %s", user_id)


async def subscribe(user_id: uuid.UUID | str) -> AsyncIterator[dict | None]:
    """Yield events for this user until the caller stops iterating.

    Yields ``None`` on each idle tick so the socket handler can send a heartbeat
    and notice a dead connection instead of blocking forever.
    """
    client = await _redis()
    if client is None:
        # No transport available. Keep the socket alive (polling carries the
        # app) but deliver nothing.
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            yield None

    pubsub = client.pubsub()
    await pubsub.subscribe(_channel(user_id))
    try:
        while True:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=_HEARTBEAT_SECONDS
            )
            if msg is None:
                yield None
                continue
            try:
                yield json.loads(msg["data"])
            except (ValueError, TypeError):
                logger.warning("realtime: dropped malformed event on %s", _channel(user_id))
    finally:
        try:
            await pubsub.unsubscribe(_channel(user_id))
            aclose = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if aclose is not None:
                res = aclose()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:  # noqa: BLE001
            pass
