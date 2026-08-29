import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session, get_db
from app.middleware.auth import get_current_user, get_current_user_id
from app.middleware.rate_limit import RateLimiter, generate_limiter

dna_read_limiter = RateLimiter(max_requests=30, window_seconds=60, prefix="dna_read")
from app.models.book_entry import BookEntry
from app.models.dna_snapshot import DNASnapshot
from app.models.user import User
from app.schemas.dna import (
    BlindSpotsResponse,
    DNAGenerateResponse,
    DNAProfileResponse,
    DNASnapshotResponse,
    EmotionalCalendarResponse,
    HeatmapResponse,
    PersonalityInfo,
    RecapResponse,
    StatsResponse,
)
from app.services.blind_spots_service import get_blind_spots
from app.services.dna_service import compute_and_cache, manual_snapshot
from app.services.profile_service import archetype_share
from app.services.calendar_service import get_emotional_calendar
from app.services.dna_engine import (
    build_heatmap_data,
    generate_recap,
    generate_stats,
)
from app.utils.cache import dna_cache, invalidate_dna

router = APIRouter(prefix="/dna", tags=["dna"])


# A `want_to_read` book was never opened: it carries no reading, no emotions, no
# arc. It must not count anywhere in the DNA aggregate — not as a heatmap column,
# not in `total_books`, not in stats. Every other status means the book was at
# least started, so it stays.
_DNA_EXCLUDED_STATUSES = ("want_to_read",)


async def _get_user_entries(db: AsyncSession, user_id) -> list[dict]:
    """Fetch a user's engaged entries and convert to dicts for the engine.

    TBR / `want_to_read` entries are excluded — they have no emotional data yet.
    """
    result = await db.execute(
        select(BookEntry)
        .options(selectinload(BookEntry.emotions))
        .where(
            BookEntry.user_id == user_id,
            BookEntry.status.notin_(_DNA_EXCLUDED_STATUSES),
        )
        .order_by(BookEntry.created_at.asc())
    )
    entries = result.scalars().all()

    return [
        {
            "id": str(e.id),
            "title": e.title,
            "author": e.author,
            "intensity": e.intensity,
            "emotions": [em.emotion_id for em in e.emotions],
            "created_at": e.created_at,
            "finished_at": e.finished_at,
        }
        for e in entries
    ]


@router.get("/profile")
async def get_dna_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The owner's DNA mirror (Phase 7): a demoted archetype, the strongest few
    falsifiable insights, the honestly-locked rest, and both recency profiles.

    Below 5 books it returns an honest "not enough yet". Served from cache while
    `dna_dirty` is false; recomputed once per change, not per request (Part 4).
    """
    cached = current_user.cached_dna_v2
    # A payload cached before a shape change is stale in a way `dna_dirty` can't
    # know about: nothing changed about the reader, only about what we serve.
    # Recompute once rather than serve a response the client can't render right.
    #   - `snapshot_count`: added as a top-level field the client depends on.
    #   - locked rows without `need`: cached before the "Not yet" copy started
    #     naming each gate's real population and the reader's count against it, so
    #     an old cache still says a bare "waits on 5 books".
    def _fresh(c: dict) -> bool:
        if "snapshot_count" not in c:
            return False
        return all("need" in row for row in (c.get("locked") or []))

    if not current_user.dna_dirty and cached and _fresh(cached):
        payload = cached
    else:
        payload = await compute_and_cache(db, current_user)

    # How many readers share this archetype moves with the POPULATION, not with
    # this reader — so it is computed per request and merged on top rather than
    # baked into their cache, where it would freeze on the day they last logged a
    # book. Two counts; None until there are enough readers to mean anything.
    return {
        **payload,
        "archetype_share": await archetype_share(db, current_user.personality_type),
    }


@router.post("/generate", response_model=DNAGenerateResponse)
async def generate_dna(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Calculate DNA and save a snapshot.

    Same engine, same gate, same answer as the DNA tab. This used to run the legacy
    engine at a 3-book gate, which meant a reader could be told "not enough yet" on
    /dna/profile and still persist a confident archetype into their own timeline —
    a permanent record of a label the mirror never showed them.
    """
    await generate_limiter.check(request)

    # Recompute first: the snapshot is written FROM the cached payload, so the two
    # cannot drift apart.
    v2 = await compute_and_cache(db, current_user)

    if not v2.get("enough"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Need at least {v2['needed']} books with a feeling logged to "
                    f"generate DNA. You have {v2['tagged_count']}."),
        )
    if not v2.get("archetype"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not determine personality. Add more emotion tags to your entries.",
        )

    snapshot = await manual_snapshot(db, current_user, v2)
    # The cached payload was built before this snapshot existed, and it carries
    # snapshot_count / has_two_snapshots. Refresh so the DNA tab sees the snapshot
    # the reader just took rather than a count one behind.
    await compute_and_cache(db, current_user)
    await invalidate_dna(current_user.id)

    return DNAGenerateResponse(
        snapshot=DNASnapshotResponse(
            id=snapshot.id,
            personality_type=snapshot.personality_type,
            emotion_data=snapshot.emotion_data,
            book_count=snapshot.book_count,
            year=snapshot.year,
            generated_at=snapshot.generated_at or datetime.now(timezone.utc),
        ),
        personality=PersonalityInfo(**v2["archetype"]),
    )


@router.get("/heatmap", response_model=HeatmapResponse)
async def get_heatmap(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get the emotion x book heatmap matrix data."""
    await dna_read_limiter.check(request)
    cache_key = f"heatmap:{user_id}"

    # Cache hit: zero DB connections needed
    cached = await dna_cache.get(cache_key)
    if cached:
        return cached

    # Cache miss: open DB only when necessary
    async with async_session() as db:
        entries = await _get_user_entries(db, user_id)
    result = build_heatmap_data(entries)
    await dna_cache.set(cache_key, result)
    return result


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Get reading statistics."""
    await dna_read_limiter.check(request)
    cache_key = f"stats:{user_id}"

    # Cache hit: zero DB connections needed
    cached = await dna_cache.get(cache_key)
    if cached:
        return cached

    # Cache miss: open DB only when necessary
    async with async_session() as db:
        entries = await _get_user_entries(db, user_id)
    result = generate_stats(entries)
    await dna_cache.set(cache_key, result)
    return result


@router.get("/patterns")
async def get_patterns(
    request: Request,
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Stats + heatmap in one response (B5.4), backing the merged 'Patterns' view
    so it's one round trip. Reuses the same cache keys as /stats and /heatmap."""
    await dna_read_limiter.check(request)
    stats = await dna_cache.get(f"stats:{user_id}")
    heatmap = await dna_cache.get(f"heatmap:{user_id}")

    if stats is None or heatmap is None:
        async with async_session() as db:
            entries = await _get_user_entries(db, user_id)
        if stats is None:
            stats = generate_stats(entries)
            await dna_cache.set(f"stats:{user_id}", stats)
        if heatmap is None:
            heatmap = build_heatmap_data(entries)
            await dna_cache.set(f"heatmap:{user_id}", heatmap)

    return {"stats": stats, "heatmap": heatmap}


@router.get("/evolution")
async def get_dna_evolution(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The evolution timeline (B7.4): one point per snapshot, oldest first. Powers
    the "watch your DNA change" screen. O(read snapshots) — the past is never
    recomputed."""
    snaps = (await db.execute(
        select(DNASnapshot)
        .where(DNASnapshot.user_id == current_user.id)
        .order_by(DNASnapshot.generated_at.asc())
    )).scalars().all()

    points = []
    for s in snaps:
        data = s.emotion_data or {}
        vec = data.get("current_vector")
        if vec:
            top = [slug for slug, _ in sorted(vec.items(), key=lambda kv: kv[1], reverse=True)[:3]
                   if _ > 0]
        else:  # legacy snapshot shape
            top = [t.get("emotion_id") for t in (data.get("top_emotions") or [])[:3]]
        points.append({
            "id": str(s.id),
            "date": s.generated_at,
            "archetype": s.personality_type,
            "dna_type_slug": s.dna_type_slug,
            "book_count": s.book_count,
            "top_emotions": top,
            "drift_from_prev": data.get("drift"),
            "trigger": s.trigger,
        })
    return points


@router.get("/history", response_model=list[DNASnapshotResponse])
async def get_dna_history(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get past DNA snapshots (for yearly Wrapped-style comparisons)."""
    result = await db.execute(
        select(DNASnapshot)
        .where(DNASnapshot.user_id == current_user.id)
        .order_by(DNASnapshot.generated_at.desc())
    )
    snapshots = result.scalars().all()
    return snapshots


@router.get("/recap", response_model=RecapResponse)
async def get_monthly_recap(
    month: str = Query(pattern=r"^\d{4}-\d{2}$", description="YYYY-MM format"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get a monthly recap — aggregated stats for a specific month.
    Includes books logged, emotion breakdown, new emotions discovered,
    and whether your personality type shifted.

    Usage: GET /api/dna/recap?month=2026-02
    """
    from datetime import datetime, timezone
    from calendar import monthrange

    # Parse month
    try:
        year, mo = int(month[:4]), int(month[5:7])
        month_start = datetime(year, mo, 1, tzinfo=timezone.utc)
        last_day = monthrange(year, mo)[1]
        month_end = datetime(year, mo, last_day, 23, 59, 59, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid month format. Use YYYY-MM (e.g. 2026-02).",
        )

    # Fetch all user entries
    all_entries = await _get_user_entries(db, current_user.id)

    # Split into month entries and prior entries
    month_entries = []
    prior_entries = []
    for e in all_entries:
        created = e.get("created_at")
        if not created:
            continue
        if month_start <= created <= month_end:
            month_entries.append(e)
        elif created < month_start:
            prior_entries.append(e)

    recap = generate_recap(
        month_entries=month_entries,
        prior_entries=prior_entries,
        current_personality=current_user.personality_type,
    )
    recap["month"] = month

    return recap


# REMOVED (audit-v2 P1-NEW-2): GET /dna/twin is unmounted. Twin (reader-matching)
# is parked (Phase 5, blueprint), and the old endpoint was O(all public users ×
# all their entries) uncached per request — a live, unbounded cost for a shelved
# feature. When Twin is reopened it must use precomputed emotion vectors
# (cached_dna_profile) + a candidate pipeline, not this. Service helpers
# (find_twins, cosine_similarity) are retained for that future rebuild.


@router.get("/emotional-calendar", response_model=EmotionalCalendarResponse)
async def emotional_calendar(
    months: int = Query(default=6, ge=1, le=36),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-month emotion distribution over the last N months. Weights sum to 1.0 per month."""
    return await get_emotional_calendar(db, current_user.id, months)


@router.get("/blind-spots", response_model=BlindSpotsResponse)
async def blind_spots(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Up to 3 emotions the user under-tags or has never tagged. Requires >=5 entries."""
    return await get_blind_spots(db, current_user.id)