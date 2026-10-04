"""WebSocket control plane (FR-08, FR-09, design §10).

Uses Starlette's synchronous TestClient because the async HTTP client cannot
open a WebSocket. The dependency overrides are the same ones the rest of the
suite uses, so these run against the same in-memory database.
"""

import asyncio

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from services.common.db import Base, get_session, get_session_factory
from services.main import app
from services.realtime.livekit import MediaGrant, create_access_token, decode_access_token
from services.realtime.protocol import ClientEvent, ServerEvent
from services.session.service import OBSERVER_LABEL


@pytest.fixture
def ws_client(tmp_path, events):
    """Synchronous client for WebSocket tests.

    `TestClient` drives the app on its own event loop in a worker thread, so it
    cannot share the rest of the suite's in-memory SQLite connection — one
    aiosqlite connection belongs to one loop. A file-backed database with
    `NullPool` sidesteps that: every operation opens its own connection on
    whichever loop is running it.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'ws.db'}"
    engine = create_async_engine(url, poolclass=NullPool)

    async def create_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create_tables())
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_session_factory] = lambda: factory
    # No context manager: entering it would run the lifespan and start the three
    # background loops, which these tests neither need nor want.
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _register(client: TestClient, email: str, role: str = "candidate") -> str:
    payload = {
        "email": email,
        "full_name": email.split("@")[0],
        "password": "correct-horse-battery",
        "role": role,
    }
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": payload["password"]}
    )
    return login.json()["access_token"]


@pytest.fixture
def joined_session(ws_client: TestClient):
    """A session with two joined candidates, ready to connect sockets."""
    host_token = _register(ws_client, "wshost@example.com", role="host")
    host_auth = {"Authorization": f"Bearer {host_token}"}
    created = ws_client.post(
        "/api/v1/sessions",
        json={
            "topic": "Should governments regulate generative AI?",
            "min_participants": 2,
            "max_participants": 4,
            "duration_seconds": 900,
            "preparation_seconds": 0,
        },
        headers=host_auth,
    )
    assert created.status_code == 201, created.text
    session = created.json()

    tokens = {}
    for name in ("wsa@example.com", "wsb@example.com"):
        token = _register(ws_client, name)
        headers = {"Authorization": f"Bearer {token}"}
        joined = ws_client.post(
            "/api/v1/lobbies/join", json={"code": session["code"]}, headers=headers
        )
        assert joined.status_code == 200, joined.text
        tokens[name] = token
    return session, tokens, host_auth


def test_a_socket_without_a_token_is_rejected(ws_client: TestClient, joined_session):
    session, _tokens, _host = joined_session
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/ws/sessions/{session['id']}"):
            pass


def test_a_socket_with_a_bad_token_is_rejected(ws_client: TestClient, joined_session):
    session, _tokens, _host = joined_session
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/ws/sessions/{session['id']}?token=not-a-jwt"):
            pass


def test_a_non_participant_cannot_connect(ws_client: TestClient, joined_session):
    session, _tokens, _host = joined_session
    outsider = _register(ws_client, "outsider@example.com")
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/ws/sessions/{session['id']}?token={outsider}"):
            pass


def test_connecting_delivers_a_state_snapshot(ws_client: TestClient, joined_session):
    """A reconnecting client needs the current picture in one frame."""
    session, tokens, _host = joined_session
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        frame = socket.receive_json()

    assert frame["type"] == ServerEvent.STATE_SNAPSHOT
    payload = frame["payload"]
    assert payload["you"] == "A"
    assert payload["session_state"] == "WAITING"
    assert payload["lobby"]["occupancy"] == 2
    assert payload["timer"]["phase"] == "waiting"
    # FR-11: the topic is still withheld in the lobby.
    assert payload["topic"]["released"] is False


def test_a_join_is_broadcast_to_the_others(ws_client: TestClient, joined_session):
    """FR-09 USER_JOINED."""
    session, tokens, _host = joined_session
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as first:
        first.receive_json()  # A's own snapshot
        with ws_client.websocket_connect(
            f"/ws/sessions/{session['id']}?token={tokens['wsb@example.com']}"
        ) as second:
            second.receive_json()  # B's own snapshot
            frame = first.receive_json()

    assert frame["type"] == ServerEvent.USER_JOINED
    assert frame["payload"]["candidate_label"] == "B"


def test_readiness_over_the_socket_updates_the_lobby(ws_client: TestClient, joined_session):
    """FR-07 driven from the control plane rather than HTTP."""
    session, tokens, host_auth = joined_session
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        socket.receive_json()
        socket.send_json({"type": ClientEvent.READY, "payload": {}})
        frame = socket.receive_json()

    assert frame["type"] == ServerEvent.USER_READY
    assert frame["payload"]["ready"] is True
    assert frame["payload"]["ready_count"] == 1
    assert frame["payload"]["start_condition_met"] is False

    lobby = ws_client.get(f"/api/v1/lobbies/{session['id']}", headers=host_auth)
    assert lobby.json()["ready_count"] == 1


def test_closing_a_socket_leaves_the_session_running(ws_client: TestClient, joined_session):
    """Design §9 / §38: a dropped candidate must not end the GD.

    The participant's own transition to DISCONNECTED is covered in
    `test_session_lifecycle.py` — `TestClient` cancels the handler's cleanup on
    teardown, so this asserts the property it can observe reliably: the session
    and the roster survive a socket closing.
    """
    session, tokens, host_auth = joined_session
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        socket.receive_json()

    lobby = ws_client.get(f"/api/v1/lobbies/{session['id']}", headers=host_auth).json()
    assert lobby["session_state"] == "WAITING"
    assert lobby["occupancy"] == 2
    assert {p["candidate_label"] for p in lobby["participants"]} == {"A", "B"}


def test_a_second_socket_for_the_same_user_is_allowed(ws_client: TestClient, joined_session):
    """Two browser tabs are two connections, not a conflict."""
    session, tokens, _host = joined_session
    url = f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    with ws_client.websocket_connect(url) as first:
        first.receive_json()
        with ws_client.websocket_connect(url) as second:
            frame = second.receive_json()
    assert frame["type"] == ServerEvent.STATE_SNAPSHOT


def test_an_unknown_client_event_gets_an_error_frame(ws_client: TestClient, joined_session):
    session, tokens, _host = joined_session
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        socket.receive_json()
        socket.send_json({"type": "DEFINITELY_NOT_AN_EVENT", "payload": {}})
        frame = socket.receive_json()

    assert frame["type"] == ServerEvent.ERROR
    assert frame["payload"]["code"] == "unknown_event"


# --------------------------------------------------------------------------
# LiveKit media tokens (FR-08)
# --------------------------------------------------------------------------


def test_a_media_token_verifies_and_grants_the_right_room():
    token = create_access_token(
        MediaGrant(room="gd-test", identity="A", display_name="Candidate A")
    )
    claims = decode_access_token(token)

    assert claims["sub"] == "A"
    assert claims["name"] == "Candidate A"
    assert claims["video"] == {
        "room": "gd-test",
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
    }
    assert claims["exp"] > claims["nbf"]


def test_a_media_token_is_rejected_under_a_different_secret():
    import jwt

    token = create_access_token(MediaGrant(room="gd-test", identity="A", display_name="A"))
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "a-different-secret", algorithms=["HS256"])


def test_the_host_can_connect_without_being_a_candidate(ws_client: TestClient, joined_session):
    """Regression: the host's own socket used to be closed with 4403."""
    session, _tokens, _host_auth = joined_session
    token = ws_client.post(
        "/api/v1/auth/login",
        json={"email": "wshost@example.com", "password": "correct-horse-battery"},
    ).json()["access_token"]

    with ws_client.websocket_connect(f"/ws/sessions/{session['id']}?token={token}") as socket:
        snapshot = socket.receive_json()
        assert snapshot["payload"]["you"] == OBSERVER_LABEL
        # Readiness and speech belong to candidates, and the host is not one.
        socket.send_json({"type": ClientEvent.READY, "payload": {}})
        assert socket.receive_json()["payload"]["code"] == "not_a_candidate"


def test_starting_the_session_is_pushed_to_candidates(ws_client: TestClient, joined_session):
    """Regression: only the host's own page moved to the room on start; the
    candidates' lobbies never heard about it and stayed put."""
    session, tokens, host_auth = joined_session
    for token in tokens.values():
        ready = ws_client.post(
            f"/api/v1/lobbies/{session['id']}/ready",
            json={"ready": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ready.status_code == 200, ready.text

    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        socket.receive_json()  # snapshot
        started = ws_client.post(f"/api/v1/sessions/{session['id']}/start", headers=host_auth)
        assert started.status_code == 200, started.text
        # The SESSION_STARTING notification frame may arrive first.
        types = [socket.receive_json() for _ in range(2)]

    frame = next(f for f in types if f["type"] == ServerEvent.SESSION_STARTED)
    # preparation_seconds=0 in the fixture, so it goes straight to discussion.
    assert frame["payload"]["phase"] == "discussion"


def test_a_transcribed_window_is_announced_to_the_room(ws_client: TestClient, joined_session):
    """The live transcript in the room refetches on TRANSCRIPT_APPENDED."""
    session, tokens, host_auth = joined_session
    for token in tokens.values():
        ws_client.post(
            f"/api/v1/lobbies/{session['id']}/ready",
            json={"ready": True},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert ws_client.post(f"/api/v1/sessions/{session['id']}/start", headers=host_auth).is_success

    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsb@example.com']}"
    ) as socket:
        socket.receive_json()  # snapshot
        uploaded = ws_client.post(
            f"/api/v1/sessions/{session['id']}/audio",
            data={"sequence": "0", "start": "0", "end": "45"},
            files={"audio": ("chunk.webm", b"fake-opus-bytes", "audio/webm")},
            headers={"Authorization": f"Bearer {tokens['wsa@example.com']}"},
        )
        assert uploaded.status_code == 202, uploaded.text
        frame = socket.receive_json()

    assert frame["type"] == ServerEvent.TRANSCRIPT_APPENDED
    assert frame["payload"]["candidate_label"] == "A"
    assert frame["payload"]["segments"] == uploaded.json()["segments_created"] > 0
