"""Test fixtures.

The suite runs against in-memory SQLite so `pytest` works with nothing else
running — no Docker, no Postgres, no Redis. Anything Postgres-specific must be
covered by a migration test instead of leaking into these fixtures.
"""

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from services.common.config import settings

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
        self.streams: dict[str, list[dict]] = {}

    async def xadd(self, stream: str, fields: dict, **_kwargs) -> str:
        entries = self.streams.setdefault(stream, [])
        entries.append(fields)
        return f"{len(entries)}-0"

    async def ping(self) -> bool:
        return True


class EventRecorder:
    """Assertion helper over what a test published."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis

    @property
    def names(self) -> list[str]:
        return [f["event"] for entries in self._redis.streams.values() for f in entries]

    def payloads(self, event: str) -> list[dict]:
        return [
            json.loads(f["payload"])
            for entries in self._redis.streams.values()
            for f in entries
            if f["event"] == event
        ]

    def count(self, event: str) -> int:
        return self.names.count(event)


@pytest.fixture
def events(monkeypatch) -> EventRecorder:
    """Autouse via `client`; request it directly to assert on emitted events."""
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(events_module, "get_redis", lambda: fake)
    return EventRecorder(fake)


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(session_factory, events: EventRecorder) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    # The WebSocket handler and the timer ticker open their own sessions.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as http_client:
        yield http_client
    app.dependency_overrides.clear()


@pytest.fixture
def credentials() -> dict:
    return {
        "email": "candidate@example.com",
        "full_name": "Test Candidate",
        "password": "correct-horse-battery",
    }


@pytest_asyncio.fixture
async def registered(client: AsyncClient, credentials: dict) -> dict:
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def tokens(client: AsyncClient, registered: dict, credentials: dict) -> dict:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def auth_header(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def host_credentials() -> dict:
    return {
        "email": "host@example.com",
        "full_name": "Test Host",
        "password": "correct-horse-battery",
        "role": "host",
    }


@pytest_asyncio.fixture
async def host_auth(client: AsyncClient, host_credentials: dict) -> dict:
    """Registered host plus a ready-to-use Authorization header."""
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
    response = await client.post("/api/v1/sessions", json=session_payload, headers=host_auth)
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def candidate_auth(client: AsyncClient, tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest_asyncio.fixture
async def second_candidate(client: AsyncClient) -> dict:
    """A second registered candidate, so a lobby can reach its minimum."""
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
    llm = StubLLM()
    set_llm_provider(llm)
    set_stt_provider(StubSTT())
    set_object_store(FilesystemStore(root=tmp_path / "media"))
    yield llm
    set_llm_provider(None)
    set_stt_provider(None)
    set_object_store(None)


@pytest.fixture
def stub_llm(offline_providers) -> StubLLM:
    """The stub provider, for asserting on what the agents were asked."""
    return offline_providers
