"""One seat per student, the public lobby list, and per-turn tracking."""

from httpx import AsyncClient

from services.session import service as session_service
from services.session.models import GDSession


async def _create(client, auth, payload):
    return await client.post("/api/v1/sessions", json=payload, headers=auth)


async def test_student_cannot_create_second_session(
    client: AsyncClient, candidate_auth: dict, session_payload: dict
):
    assert (await _create(client, candidate_auth, session_payload)).status_code == 201
    second = await _create(client, candidate_auth, session_payload)
    assert second.status_code == 409


async def test_student_in_a_lobby_cannot_join_another(
    client: AsyncClient, candidate_auth: dict, second_candidate: dict, session_payload: dict
):
    first = (await _create(client, candidate_auth, session_payload)).json()
    other = (await _create(client, second_candidate, session_payload)).json()
    # second_candidate is seated in `other`, so joining `first` must fail ...
    blocked = await client.post(
        "/api/v1/lobbies/join", json={"code": first["code"]}, headers=second_candidate
    )
    assert blocked.status_code == 409
    # ... until they leave their own lobby.
    await client.post(f"/api/v1/lobbies/{other['id']}/leave", headers=second_candidate)
    ok = await client.post(
        "/api/v1/lobbies/join", json={"code": first["code"]}, headers=second_candidate
    )
    assert ok.status_code == 200


async def test_lobby_list_shows_open_lobbies(
    client: AsyncClient, candidate_auth: dict, second_candidate: dict, session_payload: dict
):
    made = (await _create(client, candidate_auth, session_payload)).json()
    listed = await client.get("/api/v1/lobbies", headers=second_candidate)
    assert listed.status_code == 200
    item = next(i for i in listed.json() if i["session_id"] == made["id"])
    assert item["occupancy"] == 1 and item["topic"] == session_payload["topic"]


async def test_turns_track_interruptions_and_replies(session_factory):
    from uuid import uuid4

    gd = GDSession(id=uuid4(), code="TRK123", host_id=uuid4(), topic="a topic", state="LIVE")
    track = session_service.record_speech_tracked
    async with session_factory() as db:
        await track(db, gd, "A", 0.0, 10.0)
        _, changed = await track(db, gd, "B", 5.0, 12.0)  # B cuts in on A
        assert changed[0].interrupted_label == "A"
        _, changed = await track(db, gd, "C", 15.0, 20.0)  # C answers B 3s later
        assert changed[0].reply_to_label == "B" and changed[0].interrupted_label is None
        times = await session_service.interruption_times(db, gd.id)
        assert times == {"B": [5.0]}
