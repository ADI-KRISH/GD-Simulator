"""WebSocket control plane (FR-08, FR-09, design §10).

Uses Starlette's synchronous TestClient because the async HTTP client cannot
open a WebSocket. The dependency overrides are the same ones the rest of the
suite uses, so these run against the same in-memory database.

Purpose: Tests for the WebSocket control plane and LiveKit tokens: authentication of sockets,
    state snapshots, join and ready broadcasts, observer (host) behaviour, error frames, and
    token contents.
Why needed: The control plane keeps browsers in sync in real time and must reject
    unauthenticated or unauthorised sockets, treat a dropped socket as harmless, and let the
    host watch without being a candidate. LiveKit tokens must verify and grant exactly the right
    room access.
Contains:
  - ws_client: synchronous TestClient fixture backed by a file database, because the async
    client cannot open WebSockets.
  - _register / joined_session: helpers and fixture that create a host, a session and two joined
    candidates.
  - Rejection tests: no token, bad token, non-participant.
  - Behaviour tests: state snapshot on connect, join broadcast, readiness over the socket,
    closing a socket leaves the session running, two tabs allowed, unknown event gets an error
    frame.
  - Token tests: a media token verifies with the right grant and fails under another secret.
  - Regression tests: host can connect as observer, session start reaches candidates, a
    transcribed window is announced to the room.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/common/db.py, services/main.py, services/realtime/livekit.py,
    services/realtime/protocol.py, services/session/service.py
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
    # A file-backed SQLite database, so each event loop can open its own connection.
    url = f"sqlite+aiosqlite:///{tmp_path / 'ws.db'}"
    # NullPool: no connection is shared between event loops.
    engine = create_async_engine(url, poolclass=NullPool)

    async def create_tables() -> None:
        """Create all tables in the test database."""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    # Create the schema before the app handles any request.
    asyncio.run(create_tables())
    # The same session factory shape production uses.
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        """Per-request session that commits on success and rolls back on error."""
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Point the app at this test database instead of the real one.
    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_session_factory] = lambda: factory
    # No context manager: entering it would run the lifespan and start the three
    # background loops, which these tests neither need nor want.
    client = TestClient(app)
    yield client
    # Teardown: remove the overrides and dispose the engine.
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _register(client: TestClient, email: str, role: str = "candidate") -> str:
    """Register a user through the API and return their access token."""
    # A valid registration body; the display name comes from the email.
    payload = {
        "email": email,
        "full_name": email.split("@")[0],
        "password": "correct-horse-battery",
        "role": role,
    }
    # Register, then log in to obtain an access token.
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": payload["password"]}
    )
    return login.json()["access_token"]


@pytest.fixture
def joined_session(ws_client: TestClient):
    """A session with two joined candidates, ready to connect sockets."""
    # A host who creates the session.
    host_token = _register(ws_client, "wshost@example.com", role="host")
    host_auth = {"Authorization": f"Bearer {host_token}"}
    # Zero preparation, so starting goes straight to LIVE.
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

    # Two candidates register and join; their tokens are returned for the tests.
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
    """Connecting without a token is refused."""
    session, _tokens, _host = joined_session
    # The server closes the socket before accepting it.
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/ws/sessions/{session['id']}"):
            pass


def test_a_socket_with_a_bad_token_is_rejected(ws_client: TestClient, joined_session):
    """Connecting with an invalid token is refused."""
    session, _tokens, _host = joined_session
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/ws/sessions/{session['id']}?token=not-a-jwt"):
            pass


def test_a_non_participant_cannot_connect(ws_client: TestClient, joined_session):
    """A user who is not in the session cannot connect."""
    session, _tokens, _host = joined_session
    # A registered user who never joined the session.
    outsider = _register(ws_client, "outsider@example.com")
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/ws/sessions/{session['id']}?token={outsider}"):
            pass


def test_connecting_delivers_a_state_snapshot(ws_client: TestClient, joined_session):
    """A reconnecting client needs the current picture in one frame."""
    session, tokens, _host = joined_session
    # Connect as candidate A.
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        # The first frame is always the full state snapshot.
        frame = socket.receive_json()

    # Check the you, state, lobby, timer and topic fields.
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
        # B connects while A's socket is open.
        with ws_client.websocket_connect(
            f"/ws/sessions/{session['id']}?token={tokens['wsb@example.com']}"
        ) as second:
            second.receive_json()  # B's own snapshot
            # A is told that B joined.
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
        # Send READY over the socket instead of over HTTP.
        socket.send_json({"type": ClientEvent.READY, "payload": {}})
        # Everyone in the room hears USER_READY, including the sender.
        frame = socket.receive_json()

    assert frame["type"] == ServerEvent.USER_READY
    assert frame["payload"]["ready"] is True
    assert frame["payload"]["ready_count"] == 1
    assert frame["payload"]["start_condition_met"] is False

    # The database agrees with the broadcast.
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
    # Connect, then let the context manager close the socket.
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        socket.receive_json()

    # After the close the lobby is unchanged...
    lobby = ws_client.get(f"/api/v1/lobbies/{session['id']}", headers=host_auth).json()
    # ...still WAITING, with both candidates seated.
    assert lobby["session_state"] == "WAITING"
    assert lobby["occupancy"] == 2
    assert {p["candidate_label"] for p in lobby["participants"]} == {"A", "B"}


def test_a_second_socket_for_the_same_user_is_allowed(ws_client: TestClient, joined_session):
    """Two browser tabs are two connections, not a conflict."""
    session, tokens, _host = joined_session
    # One URL used for both connections.
    url = f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    # The first browser tab.
    with ws_client.websocket_connect(url) as first:
        first.receive_json()
        # A second tab for the same user.
        with ws_client.websocket_connect(url) as second:
            frame = second.receive_json()
    assert frame["type"] == ServerEvent.STATE_SNAPSHOT


def test_an_unknown_client_event_gets_an_error_frame(ws_client: TestClient, joined_session):
    """An unsupported client event is answered with an ERROR frame."""
    session, tokens, _host = joined_session
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        socket.receive_json()
        # A made-up event type.
        socket.send_json({"type": "DEFINITELY_NOT_AN_EVENT", "payload": {}})
        frame = socket.receive_json()

    # The server answers with an ERROR frame instead of dropping the connection.
    assert frame["type"] == ServerEvent.ERROR
    assert frame["payload"]["code"] == "unknown_event"


# --------------------------------------------------------------------------
# LiveKit media tokens (FR-08)
# --------------------------------------------------------------------------


def test_a_media_token_verifies_and_grants_the_right_room():
    """A LiveKit token decodes with the right identity, name and room grant."""
    # Mint a token for candidate A in room gd-test.
    token = create_access_token(
        MediaGrant(room="gd-test", identity="A", display_name="Candidate A")
    )
    # Decode it with the same secret.
    claims = decode_access_token(token)

    assert claims["sub"] == "A"
    assert claims["name"] == "Candidate A"
    # The grant names the room and allows join, publish, subscribe and data.
    assert claims["video"] == {
        "room": "gd-test",
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
    }
    # It expires after it becomes valid.
    assert claims["exp"] > claims["nbf"]


def test_a_media_token_is_rejected_under_a_different_secret():
    """A LiveKit token fails verification under a different secret."""
    # Local import: only needed to try the wrong secret.
    import jwt

    token = create_access_token(MediaGrant(room="gd-test", identity="A", display_name="A"))
    # Verification with the wrong secret must fail.
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "a-different-secret", algorithms=["HS256"])


def test_the_host_can_connect_without_being_a_candidate(ws_client: TestClient, joined_session):
    """Regression: the host's own socket used to be closed with 4403."""
    session, _tokens, _host_auth = joined_session
    # Log in as the host, who holds no participant seat.
    token = ws_client.post(
        "/api/v1/auth/login",
        json={"email": "wshost@example.com", "password": "correct-horse-battery"},
    ).json()["access_token"]

    # The host connects as the HOST observer.
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
    # Both candidates ready up over HTTP so the host can start.
    for token in tokens.values():
        ready = ws_client.post(
            f"/api/v1/lobbies/{session['id']}/ready",
            json={"ready": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ready.status_code == 200, ready.text

    # Candidate A is connected when the host starts.
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsa@example.com']}"
    ) as socket:
        socket.receive_json()  # snapshot
        # The host starts the session over HTTP.
        started = ws_client.post(f"/api/v1/sessions/{session['id']}/start", headers=host_auth)
        assert started.status_code == 200, started.text
        # The SESSION_STARTING notification frame may arrive first.
        types = [socket.receive_json() for _ in range(2)]

    # Pick out the SESSION_STARTED frame.
    frame = next(f for f in types if f["type"] == ServerEvent.SESSION_STARTED)
    # preparation_seconds=0 in the fixture, so it goes straight to discussion.
    assert frame["payload"]["phase"] == "discussion"


def test_a_transcribed_window_is_announced_to_the_room(ws_client: TestClient, joined_session):
    """The live transcript in the room refetches on TRANSCRIPT_APPENDED."""
    session, tokens, host_auth = joined_session
    # Ready up, then start the session so it is LIVE.
    for token in tokens.values():
        ws_client.post(
            f"/api/v1/lobbies/{session['id']}/ready",
            json={"ready": True},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert ws_client.post(f"/api/v1/sessions/{session['id']}/start", headers=host_auth).is_success

    # Candidate B listens on the socket.
    with ws_client.websocket_connect(
        f"/ws/sessions/{session['id']}?token={tokens['wsb@example.com']}"
    ) as socket:
        socket.receive_json()  # snapshot
        # Candidate A uploads a 45-second audio chunk over HTTP.
        uploaded = ws_client.post(
            f"/api/v1/sessions/{session['id']}/audio",
            data={"sequence": "0", "start": "0", "end": "45"},
            files={"audio": ("chunk.webm", b"fake-opus-bytes", "audio/webm")},
            headers={"Authorization": f"Bearer {tokens['wsa@example.com']}"},
        )
        assert uploaded.status_code == 202, uploaded.text
        # B's socket receives the announcement.
        frame = socket.receive_json()

    assert frame["type"] == ServerEvent.TRANSCRIPT_APPENDED
    # It names the speaker and how many segments were created.
    assert frame["payload"]["candidate_label"] == "A"
    assert frame["payload"]["segments"] == uploaded.json()["segments_created"] > 0
