"""Local composition root.

Every service package is mounted into one FastAPI process listening on
http://localhost:8000. There is no gateway and no deploy target — see
"Deployment — LOCAL ONLY" in claude.md.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text

from services.auth.router import router as auth_router
from services.common.config import settings
from services.common.db import engine
from services.common.errors import DomainError
from services.common.logging import configure_logging
from services.common.redis_client import close_redis, ping_redis
from services.evaluation.router import router as evaluation_router
from services.evaluation.worker import run_worker
from services.lobby.router import router as lobby_router
from services.metrics.router import router as metrics_router
from services.notifications.router import router as notifications_router
from services.observer.service import run_observer_loop
from services.realtime.timer import run_ticker
from services.realtime.ws import router as realtime_router
from services.session.router import router as session_router
from services.simulation import router as simulation_router
from services.speech.router import router as speech_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    logger.info(
        "backend starting",
        extra={"env": settings.app_env, "url": f"http://{settings.api_host}:{settings.api_port}"},
    )
    # Three background loops run in-process: one instance of everything, no
    # scheduler and no separate worker service (claude.md "Deployment - LOCAL
    # ONLY"). None of them is on the request path, and each contains its own
    # failures, so a stalled loop degrades one capability rather than the app.
    background = [
        asyncio.create_task(run_ticker(), name="session-timer"),
        asyncio.create_task(run_observer_loop(), name="observer-loop"),
        asyncio.create_task(run_worker(), name="evaluation-worker"),
    ]
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await close_redis()
        await engine.dispose()


app = FastAPI(
    title="GD Platform API",
    version="0.1.0",
    description="Group Discussion platform with agentic AI evaluation. Local development only.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness plus dependency status. Never fails the request when a
    dependency is down — the caller decides what to do with `degraded`."""
    checks = {"database": False, "redis": False}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:
        logger.warning("database health check failed", extra={"error": str(exc)})

    checks["redis"] = await ping_redis()

    return {
        "status": "ok" if all(checks.values()) else "degraded",
        "env": settings.app_env,
        "checks": checks,
    }


app.include_router(auth_router)
app.include_router(session_router)
app.include_router(lobby_router)
app.include_router(speech_router)
app.include_router(metrics_router)
app.include_router(evaluation_router)
app.include_router(notifications_router)
app.include_router(realtime_router)
app.include_router(simulation_router)
