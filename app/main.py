import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.middleware.error_handlers import register_error_handlers, setup_logging
from app.routers import auth, entries, dna, public, user, books, admin, mirror, meta, echo, social, notifications, profile, prompts, journal, resonance, threads, push, realtime

settings = get_settings()
setup_logging(settings.ENVIRONMENT)
logger = logging.getLogger("bibliome.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    from app.database import engine

    # Verify DB is reachable before accepting traffic
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Starting %s API (%s) — DB connection verified", settings.APP_NAME, settings.ENVIRONMENT)

    yield

    # Release DB pool cleanly on shutdown
    await engine.dispose()
    logger.info("Shutting down %s API — DB pool closed", settings.APP_NAME)


_is_prod = settings.ENVIRONMENT == "production"

# The interactive docs enumerate every route, including /api/admin/*. Useful in
# dev, an inventory for anyone poking at prod.
app = FastAPI(
    title=settings.APP_NAME,
    description="The emotional fingerprint of your reading life",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

register_error_handlers(app)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    # Spelled out rather than "*". With allow_credentials the wildcard is the
    # riskier default: it lets an allowed origin drive any method and send any
    # header with the session attached. These are what the client actually uses
    # — Content-Type and Authorization are the only headers apiFetch sets, and
    # the routers only ever expose these five verbs.
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# Routers
app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(entries.router, prefix=settings.API_V1_PREFIX)
app.include_router(dna.router, prefix=settings.API_V1_PREFIX)
app.include_router(public.router, prefix=settings.API_V1_PREFIX)
app.include_router(user.router, prefix=settings.API_V1_PREFIX)
app.include_router(books.router, prefix=settings.API_V1_PREFIX)
app.include_router(admin.router, prefix=settings.API_V1_PREFIX)
app.include_router(mirror.router, prefix=settings.API_V1_PREFIX)
app.include_router(meta.router, prefix=settings.API_V1_PREFIX)
app.include_router(echo.router, prefix=settings.API_V1_PREFIX)
app.include_router(social.router, prefix=settings.API_V1_PREFIX)
app.include_router(notifications.router, prefix=settings.API_V1_PREFIX)
app.include_router(profile.router, prefix=settings.API_V1_PREFIX)
app.include_router(prompts.router, prefix=settings.API_V1_PREFIX)
app.include_router(journal.router, prefix=settings.API_V1_PREFIX)
app.include_router(resonance.router, prefix=settings.API_V1_PREFIX)
app.include_router(threads.router, prefix=settings.API_V1_PREFIX)
app.include_router(push.router, prefix=settings.API_V1_PREFIX)
app.include_router(realtime.router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
async def health_check(response: Response):
    """Liveness *and* readiness: uptime monitoring that only checks the process
    reports green while Postgres is down, which is the outage that matters.
    """
    from app.database import async_session

    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "app": settings.APP_NAME, "db": "up"}
    except Exception:
        logger.exception("Health check failed: database unreachable")
        response.status_code = 503
        return {"status": "degraded", "app": settings.APP_NAME, "db": "down"}