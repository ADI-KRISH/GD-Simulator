"""GD as part of the platform (M14 wrap-up Day 4): its sign-in and its skill graph."""

import json
import re
import uuid
from types import SimpleNamespace

import httpx
import pytest
from ai_core.evidence import EvidenceRef
from httpx import AsyncClient

from services.ai_platform import skill_graph
from services.ai_platform.bridge import platform_email, platform_subject
from services.common.config import REPO_ROOT, settings
from services.speech.stt import StubSTT, set_stt_provider

STUDENT = "4d0aa1c0-0000-4000-8000-000000000004"
SESSION = "6a1f6c1e-0000-4000-8000-0000000000a1"
CONSENT = "9c2b0d4e-0000-4000-8000-0000000000c1"
ARTIFACT = {"artifact_id": "1b7e5f00-0000-4000-8000-0000000000f1", "checksum": "ab" * 32}

#: M04's ontology, in its checkout beside this one.
ONTOLOGY = REPO_ROOT.parent / "Memory" / "packages" / "knowledge" / "ontology" / "concepts.yaml"


@pytest.fixture
def bridge_on(monkeypatch):
    monkeypatch.setattr(settings, "platform_bridge", True)
    monkeypatch.setenv("SERVICE_SECRET", "test-service-secret")


def _student(subject: str = STUDENT, **extra: str) -> dict[str, str]:
    return {"X-Candidate-Id": subject, "X-Role": "student", **extra}


# ── Sign-in ─────────────────────────────────────────────────────────────────


async def test_platform_sign_in_is_off_unless_the_launcher_turns_it_on(client: AsyncClient):
    response = await client.post("/api/v1/auth/platform", headers=_student())
    assert response.status_code == 404


async def test_a_platform_student_gets_one_gd_account_and_keeps_it(client: AsyncClient, bridge_on):
    first = await client.post(
        "/api/v1/auth/platform", headers=_student(**{"X-Display-Name": "Asha Rao"})
    )
    assert first.status_code == 200, first.text
    again = await client.post("/api/v1/auth/platform", headers=_student())
    assert again.status_code == 200, again.text

    who = []
    for tokens in (first.json(), again.json()):
        me = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert me.status_code == 200, me.text
        who.append(me.json())
    assert who[0]["id"] == who[1]["id"]
    assert who[0]["email"] == platform_email(STUDENT)
    # A later sign-in without a name does not rename them to "Student".
    assert who[1]["full_name"] == "Asha Rao"
    assert who[0]["role"] == "candidate"


async def test_a_name_in_any_script_arrives_whole(client: AsyncClient, bridge_on):
    """The page URI-encodes it: a header is Latin-1, and a student's name need not be."""
    tokens = await client.post(
        "/api/v1/auth/platform",
        headers=_student(**{"X-Display-Name": "%E0%A4%85%E0%A4%A8%E0%A4%B6%20Rao"}),
    )
    assert tokens.status_code == 200, tokens.text
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens.json()['access_token']}"}
    )
    assert me.json()["full_name"] == "अनश Rao"


async def test_the_platform_account_has_no_usable_password(client: AsyncClient, bridge_on):
    await client.post("/api/v1/auth/platform", headers=_student())
    for guess in ("", "Student", STUDENT, "password123"):
        response = await client.post(
            "/api/v1/auth/login", json={"email": platform_email(STUDENT), "password": guess}
        )
        assert response.status_code in (401, 422)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Candidate-Id": "placement_officer-dev", "X-Role": "placement_officer"},
        {"X-Candidate-Id": "admin-dev", "X-Role": "admin"},
    ],
)
async def test_only_a_student_the_platform_named_signs_in(client: AsyncClient, bridge_on, headers):
    response = await client.post("/api/v1/auth/platform", headers=headers)
    assert response.status_code == 401


def test_a_platform_subject_round_trips_and_nothing_else_is_one():
    assert platform_subject(platform_email(STUDENT)) == STUDENT
    assert platform_subject("candidate@example.com") is None
    assert platform_subject("@platform.example") is None
    assert platform_subject(f"{STUDENT}@platform.example.com") is None
    assert platform_subject("") is None


# ── What reaches the skill graph ───────────────────────────────────────────


def test_every_concept_gd_sends_exists_in_m04s_ontology():
    if not ONTOLOGY.exists():
        pytest.skip(f"M04 is not checked out beside this repo ({ONTOLOGY})")
    ids = set(re.findall(r"^\s*- id: (concept:\w+)", ONTOLOGY.read_text(encoding="utf-8"), re.M))
    assert set(skill_graph.DIMENSION_CONCEPTS.values()) <= ids


def test_every_mapped_dimension_is_one_gd_scores_and_each_has_its_own_concept():
    from services.evaluation.agents.definitions import AGENTS_BY_GD_TYPE

    scored = {dimension for roster in AGENTS_BY_GD_TYPE.values() for dimension in roster}
    assert set(skill_graph.DIMENSION_CONCEPTS) <= scored
    # M04 keys an observation on (session, concept, kind): a shared concept collides.
    concepts = list(skill_graph.DIMENSION_CONCEPTS.values())
    assert len(concepts) == len(set(concepts))


def test_one_observation_per_mapped_dimension_shaped_as_m04_takes_it():
    batch = skill_graph.observations_for(
        STUDENT,
        SESSION,
        {"communication": 7.5, "leadership": 9.0, "behavior": 12.0, "collaboration": -1.0},
        ARTIFACT,
        CONSENT,
    )
    by_dimension = {obs["source_dimension_key"]: obs for obs in batch}
    # leadership has no honest M04 concept and is not sent.
    assert set(by_dimension) == {"communication", "behavior", "collaboration"}
    assert by_dimension["communication"]["outcome"] == 0.75
    assert by_dimension["behavior"]["outcome"] == 1.0  # clamped
    assert by_dimension["collaboration"]["outcome"] == 0.0
    for obs in batch:
        assert obs["kind"] == "gd_contribution"
        assert obs["response_model"] == "none"  # recorded, never moves θ (EC-4.17)
        assert obs["candidate_id"] == STUDENT and obs["consent_id"] == CONSENT
        assert obs["concept_id"] == skill_graph.DIMENSION_CONCEPTS[obs["source_dimension_key"]]
        for ref in obs["evidence"]:
            EvidenceRef.model_validate(ref)  # the shared contract, not a copy of it
            assert ref["artifact_id"] == ARTIFACT["artifact_id"]


def test_own_lines_are_the_candidates_words_in_order():
    segments = [
        SimpleNamespace(candidate_id="A", start=9.0, text=" second "),
        SimpleNamespace(candidate_id="B", start=5.0, text="not mine"),
        SimpleNamespace(candidate_id="A", start=1.0, text="first"),
        SimpleNamespace(candidate_id="A", start=3.0, text="  "),
    ]
    assert skill_graph.own_lines(segments, "A") == "first\nsecond"


class FakeM04:
    """M04's three routes, recording what GD sent."""

    def __init__(self, consents: list[dict] | None = None, fail: int | None = None):
        self.consents = [{"consent_id": CONSENT, "active": True}] if consents is None else consents
        self.fail = fail
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, dict(request.headers), body))
        if self.fail:
            return httpx.Response(self.fail, json={"detail": "refused"})
        if request.url.path.endswith("/consent"):
            assert request.url.params["purpose"] == "practice"
            return httpx.Response(200, json=self.consents)
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(201, json={**ARTIFACT, "created": True})
        if request.url.path.endswith("/observations"):
            return httpx.Response(200, json=[{"recorded": True}] * len(body["observations"]))
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def test_results_go_consent_then_artifact_then_observations(bridge_on):
    m04 = FakeM04()
    async with m04.client() as client:
        sent = await skill_graph.send_candidate(
            STUDENT, SESSION, {"communication": 6.0, "behavior": 8.0}, "my words", client=client
        )
    assert sent == 2
    base = f"/v1/candidates/{STUDENT}"
    assert [(method, path) for method, path, _, _ in m04.calls] == [
        ("GET", f"{base}/consent"),
        ("POST", f"{base}/artifacts"),
        ("POST", f"{base}/observations"),
    ]
    for _, _, headers, _ in m04.calls:
        assert headers["x-service-secret"] == "test-service-secret"
        assert headers["x-candidate-id"] == STUDENT
    artifact = m04.calls[1][3]
    assert artifact == {
        "candidate_id": STUDENT,
        "session_id": SESSION,
        "artifact_type": "gd_transcript",
        "content": "my words",
    }
    assert {o["consent_id"] for o in m04.calls[2][3]["observations"]} == {CONSENT}


@pytest.mark.parametrize(
    "consents", [[], [{"consent_id": CONSENT, "active": False}]], ids=["none", "revoked"]
)
async def test_without_practice_consent_nothing_is_written(bridge_on, consents):
    m04 = FakeM04(consents=consents)
    async with m04.client() as client:
        sent = await skill_graph.send_candidate(
            STUDENT, SESSION, {"communication": 6.0}, "my words", client=client
        )
    assert sent == 0
    assert [method for method, *_ in m04.calls] == ["GET"]


@pytest.mark.parametrize(
    ("subject", "dimensions", "lines"),
    [
        (STUDENT, {"communication": 6.0}, ""),  # a silent candidate: nothing to quote
        (STUDENT, {"content": 6.0, "leadership": 7.0}, "words"),  # nothing M04 tracks
        ("m15-random-browser-id", {"communication": 6.0}, "words"),  # not a platform UUID
    ],
    ids=["silent", "unmapped", "not-a-uuid"],
)
async def test_nothing_is_sent_when_there_is_nothing_m04_could_record(
    bridge_on, subject, dimensions, lines
):
    m04 = FakeM04()
    async with m04.client() as client:
        assert (
            await skill_graph.send_candidate(subject, SESSION, dimensions, lines, client=client)
            == 0
        )
    assert m04.calls == []


@pytest.fixture
def real_stt():
    """A transcriber that is not the stub: what the platform's launcher configures."""

    class HeardIt:
        name = "openai"

    set_stt_provider(HeardIt())
    yield
    set_stt_provider(StubSTT())


def _route_m04(monkeypatch, transport):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        skill_graph.httpx,
        "AsyncClient",
        lambda real=real, transport=transport, **kwargs: real(transport=transport, **kwargs),
    )


async def test_results_reach_m04_when_the_words_were_heard(bridge_on, real_stt, monkeypatch):
    m04 = FakeM04()
    _route_m04(monkeypatch, httpx.MockTransport(m04.handler))
    score = SimpleNamespace(dimensions={"communication": 6.0, "behavior": 7.0})
    segments = [SimpleNamespace(candidate_id="A", start=0.0, text="my words")]
    assert await skill_graph.send_results(SESSION, {"A": score}, segments, {"A": STUDENT}) == 2


async def test_with_the_stub_transcriber_nothing_reaches_the_skill_graph(bridge_on, monkeypatch):
    """The stub hears speech as canned sentences: never evidence of the student."""
    m04 = FakeM04()
    _route_m04(monkeypatch, httpx.MockTransport(m04.handler))
    score = SimpleNamespace(dimensions={"communication": 6.0})
    segments = [SimpleNamespace(candidate_id="A", start=0.0, text="my words")]
    assert await skill_graph.send_results(SESSION, {"A": score}, segments, {"A": STUDENT}) == 0
    assert m04.calls == []


async def test_m04_refusing_or_down_never_reaches_the_report(bridge_on, real_stt, monkeypatch):
    score = SimpleNamespace(dimensions={"communication": 6.0})
    segments = [SimpleNamespace(candidate_id="A", start=0.0, text="my words")]
    for transport in (
        httpx.MockTransport(FakeM04(fail=503).handler),
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))),
    ):
        _route_m04(monkeypatch, transport)
        assert await skill_graph.send_results(SESSION, {"A": score}, segments, {"A": STUDENT}) == 0


async def test_results_are_not_sent_while_the_bridge_is_off():
    score = SimpleNamespace(dimensions={"communication": 6.0})
    segments = [SimpleNamespace(candidate_id="A", start=0.0, text="my words")]
    assert await skill_graph.send_results(SESSION, {"A": score}, segments, {"A": STUDENT}) == 0


def test_platform_subjects_are_uuids_the_skill_graph_accepts():
    assert skill_graph._is_uuid(STUDENT)
    assert skill_graph._is_uuid(str(uuid.uuid4()))
    assert not skill_graph._is_uuid("placement_officer-dev")


# ── What the platform's System page reads ──────────────────────────────────


async def test_standalone_gd_needs_nothing_from_the_platform_and_says_so(client: AsyncClient):
    body = (await client.get("/v1/deps")).json()
    assert body["dependencies"] == [] and body["note"]


async def test_on_the_platform_gd_names_the_skill_graph_as_its_upstream(
    client: AsyncClient, bridge_on, monkeypatch
):
    monkeypatch.setattr(settings, "m04_url", "http://127.0.0.1:9")  # nothing listens
    body = (await client.get("/v1/deps")).json()
    assert [(d["module_id"], d["available"], d["required"]) for d in body] == [
        ("M04", False, False)
    ]
    assert body[0]["on_missing"]


async def test_redis_switched_off_by_configuration_is_not_degraded(
    client: AsyncClient, db_engine, monkeypatch
):
    from services import main

    monkeypatch.setattr(main, "engine", db_engine)  # /health probes the app's own engine
    monkeypatch.setattr(settings, "redis_url", "disabled")
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["checks"]["redis"] is None
