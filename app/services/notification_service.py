"""Notification service (B4.1): classify tier, apply prefs + quiet hours + batching.

Precedence rules (blueprint Feature 5):
  - Tier 0 (security) is immediate, non-disableable, and bypasses quiet hours.
  - Tier 1/2 respect the user's per-tier toggles and quiet hours.
  - Tier 1 batches by `batch_key` so N events collapse into one item
    ("3 readers responded…") rather than N pings.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.push_service import push_to_user
from app.services.realtime_service import publish as realtime_publish
from app.models.notification import (
    Notification,
    NotificationPrefs,
    TIER_DIGEST,
    TIER_DIRECT,
    TIER_SECURITY,
)
from app.services.social_service import is_blocked_between

logger = logging.getLogger("bibliome.notifications")


async def get_or_create_prefs(db: AsyncSession, user_id: uuid.UUID) -> NotificationPrefs:
    prefs = (await db.execute(
        select(NotificationPrefs).where(NotificationPrefs.user_id == user_id)
    )).scalar_one_or_none()
    if prefs is None:
        prefs = NotificationPrefs(user_id=user_id)
        db.add(prefs)
        await db.flush()
    return prefs


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _quiet_until(now_utc: datetime, prefs: NotificationPrefs) -> datetime | None:
    """If `now` is inside the user's quiet hours, return the UTC instant they end;
    otherwise None. Handles overnight windows (e.g. 22→7)."""
    s, e = prefs.quiet_hours_start, prefs.quiet_hours_end
    if s is None or e is None or s == e:
        return None
    local = now_utc.astimezone(_tz(prefs.timezone))
    h = local.hour + local.minute / 60.0
    overnight = s > e
    in_quiet = (s <= h < e) if not overnight else (h >= s or h < e)
    if not in_quiet:
        return None
    end_today = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=e)
    if end_today <= local:
        end_today += timedelta(days=1)
    return end_today.astimezone(timezone.utc)


def _merge_batch(existing: dict, incoming: dict) -> dict:
    """Collapse a repeat tier-1 event into an existing unread notification."""
    p = dict(existing)
    p["count"] = int(p.get("count", 1)) + 1
    actors = list(p.get("actors", []))
    for actor in incoming.get("actors", []):
        if actor and actor not in actors:
            actors.append(actor)
    p["actors"] = actors[:5]
    return p


async def notify(
    db: AsyncSession,
    user_id: uuid.UUID,
    tier: int,
    kind: str,
    payload: dict,
    batch_key: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> Notification | None:
    """Create (or coalesce) a notification, honoring tier/prefs/quiet-hours/blocks.
    Returns the notification, or None if suppressed."""
    # Suppress self-notifications and anything from/to a blocked actor.
    if actor_id is not None:
        if actor_id == user_id:
            return None
        if await is_blocked_between(db, user_id, actor_id):
            return None

    now = datetime.now(timezone.utc)
    deliver_after = now

    if tier != TIER_SECURITY:
        prefs = await get_or_create_prefs(db, user_id)
        if tier == TIER_DIRECT and not prefs.reply_enabled:
            return None
        if tier == TIER_DIGEST and not prefs.digest_enabled:
            return None
        quiet_until = _quiet_until(now, prefs)
        if quiet_until is not None:
            deliver_after = quiet_until

    if batch_key is not None:
        existing = (await db.execute(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.batch_key == batch_key,
                Notification.read_at.is_(None),
            )
            .order_by(Notification.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            existing.payload = _merge_batch(existing.payload, payload)
            # Deliver at the earliest appropriate time across batched events.
            if deliver_after < existing.deliver_after.replace(tzinfo=timezone.utc):
                existing.deliver_after = deliver_after
            await db.flush()
            # No push: this event coalesced into a notification the reader has
            # not read yet, so they have already been knocked on for it. Pushing
            # again is how a five-message burst becomes five buzzes.
            #
            # Realtime IS still sent: it is a data-sync nudge, not a buzz — an
            # open thread should refresh on message #2, not just message #1.
            if deliver_after <= now:
                await realtime_publish(user_id, {"type": "notify", "kind": kind})
            return existing

    n = Notification(
        user_id=user_id, tier=tier, kind=kind, payload=payload,
        batch_key=batch_key, deliver_after=deliver_after,
    )
    db.add(n)
    await db.flush()

    # Push rides on the SAME decisions made above — prefs, blocks, self-suppression
    # and quiet hours — rather than re-deriving them. Anything that stopped a
    # notification being created has already returned; anything deferred by quiet
    # hours is not pushed now, because the point of quiet hours is the phone
    # staying silent. Best effort: a failed push must never fail the write that
    # caused it.
    if deliver_after <= now:
        try:
            await push_to_user(db, user_id, kind, payload)
        except Exception:  # noqa: BLE001 — a courtesy layer cannot break the caller
            logger.exception("push failed for notification %s", n.id)
        # Instant in-app delivery for any tab this user has open. Best-effort and
        # already swallows its own errors.
        await realtime_publish(user_id, {"type": "notify", "kind": kind})

    return n


async def list_notifications(db: AsyncSession, user_id: uuid.UUID, limit: int = 30) -> list[Notification]:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == user_id, Notification.deliver_after <= now)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    now = datetime.now(timezone.utc)
    return (await db.execute(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.deliver_after <= now,
        )
    )).scalar() or 0


async def mark_read(db: AsyncSession, user_id: uuid.UUID, ids: list[uuid.UUID] | None = None) -> None:
    stmt = update(Notification).where(
        Notification.user_id == user_id, Notification.read_at.is_(None)
    )
    if ids:
        stmt = stmt.where(Notification.id.in_(ids))
    await db.execute(stmt.values(read_at=datetime.now(timezone.utc)))
    await db.flush()
