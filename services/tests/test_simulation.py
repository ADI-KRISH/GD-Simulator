"""AI agent participants (services/simulation.py), driven by the stub LLM."""

import asyncio
import uuid

from httpx import AsyncClient

from services import simulation

AGENTS = {
    "agents": [
        {"name": "Arjun", "persona": "Dominates.", "talkativeness": "high"},
        {"name": "Chitra", "persona": "Quiet expert.", "talkativeness": "low"},
    ],
    "extra_prompt": "Keep it short.",
}


async def test_agents_join_ready_and_talk_once_live(
    client: AsyncClient, host_auth: dict, gd_session: dict, monkeypatch
):
    monkeypatch.setattr(simulation, "TURN_GAP_SECONDS", 0)
    sid = gd_session["id"]

    added = await client.post(f"/api/v1/sessions/{sid}/agents", json=AGENTS, headers=host_auth)
    assert added.status_code == 200, added.text
    lobby = added.json()
    assert [p["display_name"] for p in lobby["participants"]] == ["Arjun (AI)", "Chitra (AI)"]
    assert lobby["start_condition_met"], "agents must arrive ready so the host can start"

    started = await client.post(f"/api/v1/sessions/{sid}/start", headers=host_auth)
    assert started.status_code == 200, started.text

    lines: list = []
    for _ in range(40):  # the loop polls for LIVE once a second
        await asyncio.sleep(0.25)
        transcript = await client.get(f"/api/v1/sessions/{sid}/transcript", headers=host_auth)
        lines = transcript.json()["lines"]
        if lines:
            break
    assert lines, "no agent spoke after the session went live"
    assert lines[0]["candidate_label"] in {"A", "B"}

    # Ending the session is what silences them.
    await client.post(f"/api/v1/sessions/{sid}/end", headers=host_auth)
    task = simulation._simulations[uuid.UUID(sid)].task
    await asyncio.wait_for(task, timeout=10)
    assert sid not in {str(k) for k in simulation._simulations}


async def test_only_the_host_can_add_agents(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    response = await client.post(
        f"/api/v1/sessions/{gd_session['id']}/agents", json=AGENTS, headers=candidate_auth
    )
    assert response.status_code == 403
