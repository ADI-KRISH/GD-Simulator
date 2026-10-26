"""Local composition root.

Every service package is mounted into one FastAPI process listening on
http://localhost:8114. There is no gateway and no deploy target — see
"Deployment — LOCAL ONLY" in claude.md.

Purpose:    build the one FastAPI app: start the background loops, set up CORS
            and metrics, translate domain errors to JSON, expose /health, and
            mount every service's routes.
Why needed: this is the entry point (`uvicorn services.main:app`). Without it
            nothing runs: no routes, no session timer, no Observer, no
            evaluation worker.
Contains:
    lifespan             - on startup launches the timer, Observer and evaluation
                           worker loops; on shutdown cancels them and releases
                           Redis and the database.
    app                  - the ASGI application (CORS + Prometheus /metrics).
    domain_error_handler - converts any `DomainError` to {"error": {...}} with
                           its HTTP status, so services never import HTTP.
    health               - `/health`: database and Redis status, never failing.
    include_router calls - mount auth, session, lobby, speech, metrics,
                           evaluation, notifications, realtime, simulation,
                           practice and the platform routers.
Used by:    tests/conftest and tests/test_realtime_ws (they import `app`);
            uvicorn and the launchers run it.
Depends on: ai_platform (bridge, deps), auth/lobby/metrics/notifications/
            practice/session/speech routers, evaluation (router, worker),
            observer/service, realtime (timer, ws), simulation, and
            common (config, db, errors, logging, redis_client).
"""

import asyncio  # create and cancel the background loops
import logging  # startup and health-check logs
from collections.abc import AsyncIterator  # lifespan return type
from contextlib import asynccontextmanager  # build the startup/shutdown lifespan

from fastapi import FastAPI, Request  # the app and request type for the error handler
from fastapi.middleware.cors import CORSMiddleware  # let the browser frontend call the API
from fastapi.responses import JSONResponse  # error responses
from prometheus_fastapi_instrumentator import Instrumentator  # exposes /metrics for Prometheus
from sqlalchemy import text  # raw SELECT 1 for the health check

# One router per service package, all mounted into this single process.
from services.ai_platform.bridge import router as platform_router  # platform sign-in
from services.ai_platform.deps import router as platform_deps_router  # /v1/deps
from services.auth.router import router as auth_router  # register/login/profile
from services.common.config import settings  # host/port, CORS origins, redis switch
from services.common.db import engine  # disposed on shutdown, probed by /health
from services.common.errors import DomainError  # mapped to JSON by the handler below
from services.common.logging import configure_logging  # JSON logs
from services.common.redis_client import DISABLED as REDIS_DISABLED  # "redis is switched off"
from services.common.redis_client import close_redis, ping_redis  # shutdown and health
from services.evaluation.router import router as evaluation_router  # scores and reports
from services.evaluation.worker import run_worker  # background evaluation worker
from services.lobby.router import router as lobby_router  # lobby browse/join/ready
from services.metrics.router import router as metrics_router  # metrics and observer state
from services.notifications.router import router as notifications_router  # in-app inbox
from services.observability.router import router as observability_router  # tracing dashboard API
from services.observer.service import run_observer_loop  # background Observer loop
from services.practice.router import router as practice_router  # solo practice vs AI personas
from services.realtime.timer import run_ticker  # background session clock
from services.realtime.ws import router as realtime_router  # control-plane WebSocket
from services.session.router import router as session_router  # session lifecycle
from services.simulation import router as simulation_router  # AI-agent demo sessions
from services.speech.router import router as speech_router  # audio upload and transcript

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background loops on startup; stop them and release resources on shutdown."""
    # Startup: switch logging to JSON before anything else logs.
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
        # The app serves requests while we are suspended here.
        yield
    finally:
        # Shutdown: stop the loops first, then release shared connections.
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await close_redis()
        await engine.dispose()


# The ASGI app served by uvicorn (`services.main:app`).
app = FastAPI(
    title="GD Platform API",
    version="0.1.0",
    description="Group Discussion platform with agentic AI evaluation. Local development only.",
    lifespan=lifespan,
)

# Let the browser frontend (a different origin/port) call this API with cookies/tokens.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus scrapes /metrics (see infra/prometheus/prometheus.yml).
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Translate any DomainError into {"error": {code, message}} with its HTTP status."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness plus dependency status. Never fails the request when a
    dependency is down — the caller decides what to do with `degraded`."""
    checks: dict[str, bool | None] = {"database": False, "redis": False}
    # Database check: a trivial query proves the connection works.
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:
        logger.warning("database health check failed", extra={"error": str(exc)})

    # Switched off by configuration (no-Docker mode) is not a fault: every
    # caller already runs without it, so it does not make the service degraded.
    redis_off = settings.redis_url == REDIS_DISABLED
    checks["redis"] = None if redis_off else await ping_redis()

    # Overall status ignores checks that are intentionally off (None).
    return {
        "status": "ok" if all(v for v in checks.values() if v is not None) else "degraded",
        "env": settings.app_env,
        "checks": checks,
    }


# Mount every service's routes on the one app.
app.include_router(auth_router)
app.include_router(session_router)
app.include_router(lobby_router)
app.include_router(speech_router)
app.include_router(metrics_router)
app.include_router(evaluation_router)
app.include_router(notifications_router)
app.include_router(realtime_router)
app.include_router(simulation_router)
app.include_router(practice_router)
app.include_router(observability_router)
app.include_router(platform_router)
app.include_router(platform_deps_router)
