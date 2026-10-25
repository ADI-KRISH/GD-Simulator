"""Test fixtures.

The suite runs against in-memory SQLite so `pytest` works with nothing else
running — no Docker, no Postgres, no Redis. Anything Postgres-specific must be
covered by a migration test instead of leaking into these fixtures.

Purpose:    shared pytest fixtures: an in-memory database, an in-process API
            client, ready-made users and sessions, a fake Redis, and offline
            stand-ins for the LLM, speech-to-text and file storage.
Why needed: every API/integration test needs the same setup; centralising it
            keeps tests short and guarantees none of them touch real services.
Contains:
    FakeRedis, EventRecorder - capture published events so tests can assert on them.
    events                   - fixture wiring the fake Redis into the event bus.
    db_engine, session_factory - fresh SQLite database with all tables.
    client                   - HTTP client bound to the app and that database.
    credentials, registered, tokens, auth_header - a default signed-in candidate.
    host_credentials, host_auth - a signed-in host.
    session_payload, gd_session - a valid technical GD session created by the host.
    candidate_auth, second_candidate - extra candidates for lobby tests.
    clean_hub                - resets the WebSocket hub between tests (autouse).
    offline_providers, stub_llm - force stub LLM/STT and temp-dir storage (autouse).
Used by:    pytest, for every test module under services/tests.
Depends on: auth, common (config, db, events, redis_client, storage),
            evaluation, llm (+ stub provider), main, notifications, session,
            speech (models, stt).
"""

import json
from collections.abc import AsyncIterator  # type of the async client fixture

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient  # in-process API client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool  # one shared connection for the in-memory database

from services.common.config import settings  # settings, patched before the app imports

# Real bcrypt cost on every fixture user would dominate the suite runtime; the
# hashing itself is covered directly in test_security.py. This has to happen
# before the service modules below import anything that reads the setting,
# hence the deliberate mid-file imports.
settings.bcrypt_rounds = 4

from services.auth import models as auth_models  # noqa: F401,E402  (registers tables)
from services.common import events as events_module  # noqa: E402
from services.common import redis_client  # noqa: E402
from services.common.db import Base, get_session, get_session_factory  # noqa: E402
from services.common.storage import FilesystemStore, set_object_store  # noqa: E402
from services.evaluation import models as evaluation_models  # noqa: F401,E402  (registers tables)
from services.llm import set_llm_provider  # noqa: E402
from services.llm.stub_provider import StubLLM  # noqa: E402
from services.main import app  # noqa: E402
from services.notifications import models as notification_models  # noqa: F401,E402
from services.session import models as session_models  # noqa: F401,E402  (registers tables)
from services.speech import models as speech_models  # noqa: F401,E402  (registers tables)
from services.speech.stt import StubSTT, set_stt_provider  # noqa: E402


class FakeRedis:
    """Just enough Redis for the event bus.

    Real Redis is not running during unit tests, and `publish` deliberately
    swallows connection errors, so without this the suite would silently assert
    nothing about events while paying a connection timeout on every call.
    """

    def __init__(self) -> None:
        """Start with no streams."""
        self.streams: dict[str, list[dict]] = {}

    async def xadd(self, stream: str, fields: dict, **_kwargs) -> str:
        """Record the entry on the named stream and return a fake message id."""
        # Append in memory so tests can read back exactly what was published.
        entries = self.streams.setdefault(stream, [])
        entries.append(fields)
        return f"{len(entries)}-0"

    async def ping(self) -> bool:
        """Always healthy."""
        return True


class EventRecorder:
    """Assertion helper over what a test published."""

    def __init__(self, redis: FakeRedis) -> None:
        """Wrap the fake Redis whose streams will be inspected."""
        self._redis = redis

    @property
    def names(self) -> list[str]:
        """Names of every event published, in stream order."""
        return [f["event"] for entries in self._redis.streams.values() for f in entries]

    def payloads(self, event: str) -> list[dict]:
        """Decoded payloads of every published event with this name."""
        return [
            json.loads(f["payload"])
            for entries in self._redis.streams.values()
            for f in entries
            if f["event"] == event
        ]

    def count(self, event: str) -> int:
        """How many times this event was published."""
        return self.names.count(event)


@pytest.fixture
def events(monkeypatch) -> EventRecorder:
    """Autouse via `client`; request it directly to assert on emitted events."""
    # Point every Redis lookup at the same in-memory fake for this test only.
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(events_module, "get_redis", lambda: fake)
    return EventRecorder(fake)


@pytest_asyncio.fixture
async def db_engine():
    """A fresh in-memory SQLite database with every table created."""
    # StaticPool keeps one connection alive, otherwise the in-memory DB vanishes.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    # Create all tables straight from the models (no Alembic needed in tests).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the in-memory database."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(session_factory, events: EventRecorder) -> AsyncIterator[AsyncClient]:
    """An in-process API client wired to the in-memory database."""
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        """Per-request session that commits on success and rolls back on error."""
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Swap the app's real database dependencies for the in-memory ones.
    app.dependency_overrides[get_session] = override_get_session
    # The WebSocket handler and the timer ticker open their own sessions.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    # Call the app directly in-process: no server, no network.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8114") as http_client:
        yield http_client
    # Undo the overrides so one test cannot leak its database into the next.
    app.dependency_overrides.clear()


@pytest.fixture
def credentials() -> dict:
    """Sign-up details for the default test candidate."""
    return {
        "email": "candidate@example.com",
        "full_name": "Test Candidate",
        "password": "correct-horse-battery",
    }


@pytest_asyncio.fixture
async def registered(client: AsyncClient, credentials: dict) -> dict:
    """Register the default candidate and return the created account."""
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def tokens(client: AsyncClient, registered: dict, credentials: dict) -> dict:
    """Log the default candidate in and return their token pair."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def auth_header(tokens: dict) -> dict:
    """Authorization header for the default candidate."""
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def host_credentials() -> dict:
    """Sign-up details for the test host."""
    return {
        "email": "host@example.com",
        "full_name": "Test Host",
        "password": "correct-horse-battery",
        "role": "host",
    }


@pytest_asyncio.fixture
async def host_auth(client: AsyncClient, host_credentials: dict) -> dict:
    """Registered host plus a ready-to-use Authorization header."""
    # Register, then log in, then expose just the bearer header tests need.
    registered = await client.post("/api/v1/auth/register", json=host_credentials)
    assert registered.status_code == 201, registered.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": host_credentials["email"], "password": host_credentials["password"]},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
def session_payload() -> dict:
    """A valid body for creating a technical, advanced GD session."""
    return {
        "topic": "Should governments regulate generative AI?",
        "topic_category": "Technology / Policy",
        "gd_type": "technical",
        "difficulty": "advanced",
        "min_participants": 2,
        "max_participants": 4,
        "duration_seconds": 900,
        "preparation_seconds": 0,
    }


@pytest_asyncio.fixture
async def gd_session(client: AsyncClient, host_auth: dict, session_payload: dict) -> dict:
    """Create a session as the host and return it."""
    response = await client.post("/api/v1/sessions", json=session_payload, headers=host_auth)
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def candidate_auth(client: AsyncClient, tokens: dict) -> dict:
    """Authorization header for the default candidate (async variant)."""
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest_asyncio.fixture
async def second_candidate(client: AsyncClient) -> dict:
    """A second registered candidate, so a lobby can reach its minimum."""
    # Register and log in a different user than the default candidate.
    payload = {
        "email": "candidate2@example.com",
        "full_name": "Second Candidate",
        "password": "correct-horse-battery",
    }
    registered = await client.post("/api/v1/auth/register", json=payload)
    assert registered.status_code == 201, registered.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture(autouse=True)
def clean_hub():
    """The WebSocket hub is a process-wide singleton; isolate tests from it."""
    from services.realtime.hub import hub

    # Reset before and after so no test sees another test's connections.
    hub.reset()
    yield
    hub.reset()


@pytest.fixture(autouse=True)
def offline_providers(tmp_path, monkeypatch):
    """Pin every external integration to its offline implementation.

    The suite must run with no Docker, no Redis, no MinIO, no Whisper model and
    no LLM API key — see claude.md, "run it locally ... 'done' means it works on
    this machine". Real providers are exercised by running the app, not by the
    unit tests.
    """
    # Swap in deterministic stubs and a throwaway temp folder for audio files.
    llm = StubLLM()
    set_llm_provider(llm)
    set_stt_provider(StubSTT())
    set_object_store(FilesystemStore(root=tmp_path / "media"))
    yield llm
    # Restore defaults so provider choice never leaks between tests.
    set_llm_provider(None)
    set_stt_provider(None)
    set_object_store(None)


@pytest.fixture
def stub_llm(offline_providers) -> StubLLM:
    """The stub provider, for asserting on what the agents were asked."""
    return offline_providers
