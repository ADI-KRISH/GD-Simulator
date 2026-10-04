"""The agentic evaluation pipeline end to end (design §18–§21, §31, §33, §39).

These run against the stub LLM, so they do not test whether the scores are
*good* — they test that the pipeline is wired correctly: the right agents are
selected for the GD type, every candidate gets an evidence packet, agent output
is validated, the aggregator (not the model) produces the final number, reports
carry evidence, and reprocessing a job is idempotent.
"""

import uuid

import pytest
from httpx import AsyncClient

from services.evaluation import repository as eval_repo
from services.evaluation import service as eval_service
from services.evaluation.agents.definitions import AGENTS_BY_GD_TYPE, select_agents
from services.evaluation.supervisor import plan as supervisor_plan
from services.llm import set_llm_provider
from services.llm.stub_provider import StubLLM
from services.session import repository as session_repo
from services.session import service as session_service
from services.session.models import GDSession, GDType, Participant
from services.session.state import EvaluationState, ParticipantState, SessionState
from services.speech.service import persist_segment

# A short scripted discussion: A dominates, B pushes back, C barely speaks.
# Written so the deterministic metrics have something real to measure.
SCRIPT = [
    (
        "A",
        0,
        25,
        "I think regulation of generative AI is overdue, and the cost of inaction is high.",
    ),
    ("B", 26, 40, "I disagree, that assumes regulators can move faster than the technology."),
    (
        "A",
        41,
        70,
        "B, that is fair, but the alternative is letting harm compound. Who bears that cost?",
    ),
    ("C", 72, 78, "Could we separate model training from deployment here?"),
    (
        "A",
        79,
        110,
        "Building on C's point, deployment is where the measurable harm actually lands.",
    ),
    ("B", 111, 130, "I agree with that split, though enforcement is still the open question."),
]


async def _seed_discussion(db, gd: GDSession) -> None:
    for label, start, end, text in SCRIPT:
        await session_service.record_speech(db, gd, label, float(start), float(end))
        await persist_segment(db, gd.id, label, float(start), float(end), text, 0.93)
    await db.flush()


@pytest.fixture
async def ended_session(
    session_factory, host_auth, client: AsyncClient, gd_session, candidate_auth, second_candidate
):
    """A session taken all the way to ENDED with a real transcript behind it."""
    session_id = uuid.UUID(gd_session["id"])
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
    await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, session_id)
        # A third candidate, so the group report has someone under-participating.
        await session_repo.add_participant(
            db,
            Participant(
                session_id=gd.id,
                user_id=gd.host_id,
                candidate_label="C",
                display_name="Third Candidate",
                state=str(ParticipantState.ACTIVE),
                is_ready=True,
            ),
        )
        await _seed_discussion(db, gd)
        await session_service.end_session(db, gd, reason="test")
        await db.commit()
    return session_id


async def test_agent_selection_follows_the_gd_type(ended_session):
    """Design §22: the Supervisor picks the roster from the GD type."""
    technical = [a.name for a in select_agents(GDType.TECHNICAL)]
    leadership = [a.name for a in select_agents(GDType.LEADERSHIP)]

    assert "critical_thinking" in technical
    assert "leadership" not in technical
    assert "leadership" in leadership
    assert "content" not in leadership
    # Every roster is a real agent set, and behavior always runs.
    for names in AGENTS_BY_GD_TYPE.values():
        assert "behavior" in names


async def test_an_unknown_gd_type_falls_back_to_the_general_roster():
    assert select_agents("nonsense") == select_agents(GDType.GENERAL)


async def test_the_supervisor_builds_one_packet_per_candidate(session_factory, ended_session):
    """Design §23: shared context plus candidate-specific evidence."""
    from services.metrics import service as metrics_service
    from services.observer import service as observer_service
    from services.speech.service import get_transcript

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ended_session)
        segments = await get_transcript(db, gd.id)
        metrics = await metrics_service.build_metrics(db, gd.id)
        state = await observer_service.current_state(db, gd.id)
        participants = await session_repo.list_active_participants(db, gd.id)
        labels = [p.candidate_label for p in participants]

        plan = supervisor_plan(
            str(gd.id), gd.topic, gd.gd_type, gd.duration_seconds, labels, segments, metrics, state
        )

    assert set(plan.packets) == set(labels)
    # Per-dimension batching is one call per specialist; per-candidate is one
    # per pair. Same plan either way — only the dispatch differs.
    assert plan.call_count("per_dimension") == len(plan.agents)
    assert plan.call_count("per_candidate") == len(plan.agents) * len(labels)

    packet_a = plan.packets["A"]
    rendered = packet_a.render()
    # The packet must carry the candidate's own words and their measured numbers.
    assert "regulation of generative AI is overdue" in rendered
    assert "speaking time" in rendered
    assert packet_a.metrics.candidate_id == "A"
    # A responded to B and C responded into the thread — the interaction graph
    # is in the packet, not just in the metrics table.
    assert packet_a.responded_to or packet_a.responses_received


async def test_shared_context_is_identical_for_every_candidate(session_factory, ended_session):
    """Design §49: build it once, do not duplicate it per candidate."""
    from services.metrics import service as metrics_service
    from services.observer import service as observer_service
    from services.speech.service import get_transcript

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ended_session)
        plan = supervisor_plan(
            str(gd.id),
            gd.topic,
            gd.gd_type,
            gd.duration_seconds,
            ["A", "B", "C"],
            await get_transcript(db, gd.id),
            await metrics_service.build_metrics(db, gd.id),
            await observer_service.current_state(db, gd.id),
        )
    assert gd.topic in plan.context.render()


async def test_full_pipeline_produces_scores_and_reports(session_factory, ended_session):
    """The whole design §20 flow, from ENDED to COMPLETED."""
    async with session_factory() as db:
        job = await eval_service.run_for_session(db, ended_session)
        await db.commit()

        assert EvaluationState(job.state) is EvaluationState.COMPLETED

        gd = await session_repo.get_session_by_id(db, ended_session)
        assert SessionState(gd.state) is SessionState.COMPLETED

        scores = await eval_repo.list_scores(db, job.id)
        assert {s.candidate_label for s in scores} == {"A", "B", "C"}
        for score in scores:
            assert 0 <= score.overall <= 100
            # The weights actually applied are stored with the score.
            assert sum(score.weights.values()) == pytest.approx(1.0)

        outputs = await eval_repo.list_agent_outputs(db, job.id)
        expected_calls = len(select_agents(gd.gd_type)) * 3
        assert len(outputs) == expected_calls

        reports = await eval_repo.list_reports_for_job(db, job.id)
        # One per candidate plus the group report (design §33, §34).
        assert len(reports) == 4
        assert sum(1 for r in reports if r.candidate_label is None) == 1


async def test_every_dimension_score_carries_evidence(session_factory, ended_session):
    """NFR-08 / claude.md invariant 6: no unexplained numbers."""
    async with session_factory() as db:
        await eval_service.run_for_session(db, ended_session)
        await db.commit()
        report = await eval_repo.get_candidate_report(db, ended_session, "A")

    body = report.body
    assert body["overall_score"] >= 0
    assert body["dimensions"]
    for dimension in body["dimensions"]:
        assert dimension["evidence"], f"{dimension['dimension']} has no evidence"
        for item in dimension["evidence"]:
            assert item["timestamp"]
            assert item["summary"]


async def test_the_report_includes_measured_behaviour(session_factory, ended_session):
    """The report must show the deterministic metrics, not just model prose."""
    async with session_factory() as db:
        await eval_service.run_for_session(db, ended_session)
        await db.commit()
        report = await eval_repo.get_candidate_report(db, ended_session, "A")

    metrics = report.body["metrics"]
    assert metrics["speaking_seconds"] > 0
    assert 0 <= metrics["participation_ratio"] <= 1
    # A dominated the discussion in the script, so the feedback should say so.
    assert "%" in report.body["feedback"]


async def test_the_group_report_names_dominant_and_quiet_candidates(session_factory, ended_session):
    """Design §34."""
    async with session_factory() as db:
        await eval_service.run_for_session(db, ended_session)
        await db.commit()
        group = await eval_repo.get_group_report(db, ended_session)

    body = group.body
    assert "A" in body["dominant_participants"]
    assert "C" in body["under_participating"]
    assert body["discussion_quality"] in {"high", "moderate", "low"}
    assert body["participation"]["A"] > body["participation"]["C"]


async def test_the_final_score_is_recomputable_from_stored_evidence(session_factory, ended_session):
    """Design §32: the aggregator is deterministic, so the stored agent outputs
    plus the stored weights must reproduce the stored score exactly."""
    from services.evaluation.aggregator import aggregate_candidate

    async with session_factory() as db:
        job = await eval_service.run_for_session(db, ended_session)
        await db.commit()
        outputs = eval_service.outputs_to_contracts(await eval_repo.list_agent_outputs(db, job.id))
        scores = {s.candidate_label: s for s in await eval_repo.list_scores(db, job.id)}

    for label, stored in scores.items():
        recomputed = aggregate_candidate(label, outputs[label], job.rubric)
        assert recomputed.overall == pytest.approx(stored.overall)


async def test_requesting_evaluation_twice_reuses_the_same_job(session_factory, ended_session):
    """Design §39: idempotent on session_id + evaluation_version."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ended_session)
        first = await eval_service.request_evaluation(db, gd)
        second = await eval_service.request_evaluation(db, gd)
        await db.commit()

    assert first.id == second.id
    assert first.evaluation_version == 1


async def test_rerunning_a_job_does_not_duplicate_outputs(session_factory, ended_session):
    """Reprocessing must converge on one report, not two inconsistent ones."""
    async with session_factory() as db:
        job = await eval_service.run_for_session(db, ended_session)
        await db.commit()
        first_outputs = len(await eval_repo.list_agent_outputs(db, job.id))
        first_reports = len(await eval_repo.list_reports_for_job(db, job.id))

    async with session_factory() as db:
        job = await eval_repo.get_job(db, job.id)
        # A completed session is re-run the way a redelivered queue message
        # would arrive: same job row, same version.
        job.state = str(EvaluationState.FAILED)
        await eval_service.run_job(db, job)
        await db.commit()
        assert len(await eval_repo.list_agent_outputs(db, job.id)) == first_outputs
        assert len(await eval_repo.list_reports_for_job(db, job.id)) == first_reports


async def test_the_job_records_its_configuration_versions(session_factory, ended_session):
    """Design §40: which configuration produced this score?"""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ended_session)
        job = await eval_service.request_evaluation(db, gd)
        await db.commit()

    assert job.rubric_version
    assert job.prompt_version
    assert job.agent_version
    assert job.selected_agents == [a.name for a in select_agents(gd.gd_type)]


async def test_a_live_session_cannot_be_evaluated(
    session_factory, client, gd_session, host_auth, candidate_auth, second_candidate
):
    """Evaluation is strictly downstream of the session ending (design §18)."""
    from services.common.errors import ConflictError

    session_id = uuid.UUID(gd_session["id"])
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
    await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, session_id)
        with pytest.raises(ConflictError):
            await eval_service.request_evaluation(db, gd)


async def test_a_total_llm_failure_does_not_corrupt_the_session(session_factory, ended_session):
    """claude.md invariant 1: the AI layer failing must not damage the record."""
    set_llm_provider(StubLLM(fail=True))
    try:
        async with session_factory() as db:
            with pytest.raises(RuntimeError):
                await eval_service.run_for_session(db, ended_session)
            await db.commit()

        async with session_factory() as db:
            gd = await session_repo.get_session_by_id(db, ended_session)
            from services.speech import repository as speech_repo

            segments = await speech_repo.list_segments(db, ended_session)
            job = await eval_repo.latest_job(db, ended_session)

        # The job failed, but the session and its transcript are intact.
        assert EvaluationState(job.state) is EvaluationState.FAILED
        assert SessionState(gd.state) is SessionState.EVALUATING
        assert len(segments) == len(SCRIPT)
    finally:
        set_llm_provider(StubLLM())


async def test_a_failed_job_can_be_retried_after_the_llm_recovers(session_factory, ended_session):
    """Design §38: the queue retries; the retry must then succeed."""
    set_llm_provider(StubLLM(fail=True))
    async with session_factory() as db:
        with pytest.raises(RuntimeError):
            await eval_service.run_for_session(db, ended_session)
        await db.commit()

    set_llm_provider(StubLLM())
    async with session_factory() as db:
        job = await eval_repo.latest_job(db, ended_session)
        job = await eval_service.run_job(db, job)
        await db.commit()

    assert EvaluationState(job.state) is EvaluationState.COMPLETED
