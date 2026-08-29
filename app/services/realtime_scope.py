"""Authorizing a realtime *scope* for a user. [realtime phases 2–3]

A scope is a conversation a socket can enter to receive its presence and typing
traffic:

    collection:{uuid}   — a collection room; the user must be a member
    thread:{uuid}       — a resonance thread; the user must be one of its two parties

Called once per ``scope_enter`` frame with a fresh short-lived DB session — never
held for the life of the socket.
"""

import uuid

from app.database import async_session
from app.services.collection_service import get_visible_collection
from app.services.resonance_service import get_thread_for_user

MAX_SCOPE_LEN = 80


def parse_scope(scope: str) -> tuple[str, uuid.UUID] | None:
    if not isinstance(scope, str) or len(scope) > MAX_SCOPE_LEN or ":" not in scope:
        return None
    kind, _, raw = scope.partition(":")
    if kind not in ("collection", "thread"):
        return None
    try:
        return kind, uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


async def authorize(scope: str, user_id: uuid.UUID) -> bool:
    """True if `user_id` may enter `scope`. Any error (bad id, gone, not a
    member) is a plain False — the socket just doesn't join."""
    parsed = parse_scope(scope)
    if parsed is None:
        return False
    kind, obj_id = parsed
    try:
        async with async_session() as db:
            if kind == "collection":
                c, _member = await get_visible_collection(db, obj_id, user_id)
                return c is not None
            found = await get_thread_for_user(db, obj_id, user_id)
            return found is not None
    except Exception:  # noqa: BLE001
        return False
