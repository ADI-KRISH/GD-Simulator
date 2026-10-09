"""Evaluation pipeline (FR-20 – FR-30, design §18–§21, §31, §33).

Two entry points:

* `request_evaluation` — called when a session ends. Creates the durable job row
  and enqueues it. Fast, and it never touches an LLM.
* `run_job` — called by a worker. Walks the evaluation state machine:
  PREPARING → RUNNING → AGGREGATING → REPORT_GENERATING → COMPLETED.

Idempotency (design §39) is enforced at both ends: `request_evaluation` reuses
an existing job for the same `(session, version)` instead of creating a second,
and `run_job` clears any partial output before re-running, so a redelivered
message converges on one consistent report rather than two contradictory ones.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from services.common.config import settings
from services.common.contracts import (
    EvaluationArtifacts,
    EvaluationOutput,
    EvaluationVersions,
    RollingState,
    validate_contract,
)
from services.common.contracts import (
    EvaluationJob as EvaluationJobContract,
)
from services.common.errors import ConflictError, NotFoundError
from services.common.events import Event, publish
from services.evaluation import queue
from services.evaluation import repository as repo
from services.evaluation.agents.definitions import select_agents
from services.evaluation.aggregator import aggregate_session, normalise_weights
from services.evaluation.graph import run_graph
from services.evaluation.group_synthesis import synthesise_group
from services.evaluation.models import AgentOutput, CandidateScore, EvaluationJob, Report
from services.evaluation.report import build_candidate_report, build_group_report
from services.evaluation.supervisor import run as run_supervisor
from services.evaluation.synthesis import synthesise
from services.evaluation.tools import TurnRecord
from services.metrics import service as metrics_service
from services.notifications.models import NotificationKind
from services.notifications.service import notify
from services.observer import service as observer_service
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame
from services.session import repository as session_repo
from services.session import service as session_service
from services.session.models import GDSession
from services.session.state import (
    EvaluationState,
    SessionState,
    transition_evaluation,
    transition_session,
)
from services.speech.service import get_transcript

logger = logging.getLogger(__name__)

# Orchestration engines. `langgraph` runs the workflow as a compiled state
# graph; `native` drives the same stages with plain asyncio. Both call the same
# agents, contracts and deterministic aggregator.
ENGINE_LANGGRAPH = "langgraph"
ENGINE_NATIVE = "native"


@dataclass(slots=True)
class OrchestrationOutcome:
    """What an orchestration engine produces, whichever engine ran.

    Both engines run the same stages over the same agents and the same
    deterministic aggregator; they differ only in what drives them. Keeping the
    result shape identical is what lets `run_job` stay engine-agnostic — and
    what makes the two comparable on the same session.
    """

    outputs_by_candidate: dict[str, list[EvaluationOutput]]
    runs: list
    failures: list[str]
    conflicts: list[str]
    scores: dict
    syntheses: dict


def eval_topic(gd: GDSession) -> str:
    """The topic as the evaluators see it.

    A practice session has one candidate and non-scored AI personas; saying so
    keeps Leadership/Collaboration from penalising the lack of human peers.
    """
    if gd.mode != "solo":
        return gd.topic
    names = ", ".join(p["name"] for p in gd.personas)
    return (
        f"{gd.topic}\n[Practice session: candidate A debated AI personas ({names}), who are "
        "not scored. Judge collaboration and leadership by how A engaged with them.]"
    )


async def _orchestrate(
    gd: GDSession,
    labels: list[str],
    segments: list,
    metrics,
    state: RollingState,
    weights: dict[str, float],
    *,
    attempt: int = 1,
    evaluation_version: int = 1,
    turns: list | None = None,
    interruptions: dict[str, list[float]] | None = None,
) -> OrchestrationOutcome:
    """Run the evaluation workflow on the configured engine."""
    topic = eval_topic(gd)
    if settings.evaluation_engine.lower() == ENGINE_LANGGRAPH:
        final = await run_graph(
            session_id=str(gd.id),
            topic=topic,
            gd_type=gd.gd_type,
            duration_seconds=gd.duration_seconds,
            candidates=labels,
            segments=segments,
            metrics=metrics,
            rolling_state=state,
            weights=weights,
            attempt=attempt,
            evaluation_version=evaluation_version,
            turns=turns,
            interruptions=interruptions,
        )
        return OrchestrationOutcome(
            outputs_by_candidate=final.get("outputs_by_candidate", {}),
            runs=final.get("runs", []),
            failures=final.get("failures", []),
            conflicts=final.get("conflicts", []),
            scores=final.get("scores", {}),
            syntheses=dict(final.get("syntheses", [])),
        )

    # `native`: the same stages driven directly by asyncio.
    result = await run_supervisor(
        session_id=str(gd.id),
        topic=topic,
        gd_type=gd.gd_type,
        duration_seconds=gd.duration_seconds,
        candidates=labels,
        segments=segments,
        metrics=metrics,
        state=state,
    )
    scores = aggregate_session(result.outputs_by_candidate, weights, interruptions)
    syntheses = dict(
        zip(
            scores,
            await asyncio.gather(
                *(
                    synthesise(
                        label,
                        result.outputs_by_candidate.get(label, []),
                        score.weights or weights,
                        metrics.per_candidate[label],
                        topic,
                        segments,
                    )
                    for label, score in scores.items()
                )
            ),
            strict=True,
        )
    )
    return OrchestrationOutcome(
        outputs_by_candidate=result.outputs_by_candidate,
        runs=result.runs,
        failures=[f"{f.agent}/{f.candidate_label}: {f.error}" for f in result.failures],
        conflicts=result.conflicts,
        scores=scores,
        syntheses=syntheses,
    )


def _provenance(runs: list, output) -> dict:
    """Tool calls the specialist made and what the critic said about its verdict."""
    run = next((r for r in runs if r.output is output), None)
    return {"tool_calls": run.tool_calls, "critic": run.critic} if run else {}


async def _set_job_state(
    session: AsyncSession, job: EvaluationJob, target: EvaluationState
) -> EvaluationJob:
    job.state = str(transition_evaluation(EvaluationState(job.state), target))
    await session.flush()
    return job


def to_contract(job: EvaluationJob, topic: str, candidates: list[str]) -> EvaluationJobContract:
    """Row → the `evaluation_job` contract that travels on the queue."""
    contract = EvaluationJobContract(
        session_id=str(job.session_id),
        evaluation_version=job.evaluation_version,
        topic=topic,
        gd_type=job.gd_type,
        rubric={"weights": job.rubric},
        candidate_ids=candidates,
        artifacts=EvaluationArtifacts(
            # References, not inlined blobs (design §20): the worker reads the
            # transcript, state and metrics from the database it shares.
            transcript_ref=f"db://transcript_segments?session_id={job.session_id}",
            final_rolling_state_ref=f"db://rolling_states?session_id={job.session_id}",
            metrics_ref=f"db://speech_events?session_id={job.session_id}",
        ),
        versions=EvaluationVersions(
            rubric_version=job.rubric_version,
            prompt_version=job.prompt_version,
            agent_version=job.agent_version,
        ),
    )
    validate_contract(contract)
    return contract


async def request_evaluation(
    session: AsyncSession, gd: GDSession, *, force_new_version: bool = False
) -> EvaluationJob:
    """Create and enqueue the evaluation job for an ended session (design §18).

    Called from the session-end path, so it must be cheap and must not fail on
    an LLM or queue problem — the job row is durable regardless of whether the
    enqueue succeeds.
    """
    if SessionState(gd.state) not in {
        SessionState.ENDED,
        SessionState.EVALUATING,
        SessionState.COMPLETED,
        SessionState.FAILED,
    }:
        raise ConflictError("a session must end before it can be evaluated")

    if not force_new_version:
        existing = await repo.latest_job(session, gd.id)
        if existing is not None and EvaluationState(existing.state) not in {EvaluationState.FAILED}:
            # Idempotent: asking twice returns the same job (design §39).
            return existing

    version = await repo.next_version(session, gd.id)
    participants = await session_repo.list_active_participants(session, gd.id)
    weights = normalise_weights(gd.rubric_weights)

    job = await repo.add_job(
        session,
        EvaluationJob(
            session_id=gd.id,
            evaluation_version=version,
            state=str(EvaluationState.QUEUED),
            gd_type=gd.gd_type,
            rubric=weights,
            rubric_version=gd.rubric_version,
            prompt_version=settings.prompt_version,
            agent_version=settings.agent_version,
            selected_agents=[a.name for a in select_agents(gd.gd_type)],
        ),
    )

    if SessionState(gd.state) is SessionState.ENDED:
        gd.state = str(transition_session(SessionState.ENDED, SessionState.EVALUATING))
        await session.flush()

    contract = to_contract(job, gd.topic, [p.candidate_label for p in participants])
    await queue.enqueue(contract)
    return job


async def run_job(session: AsyncSession, job: EvaluationJob) -> EvaluationJob:
    """Execute one evaluation job end to end (design §20).

    Raises on failure so the worker can requeue. The session's own state is only
    ever advanced to COMPLETED here — a failed evaluation leaves the recorded
    discussion untouched.
    """
    gd = await session_repo.get_session_by_id(session, job.session_id)
    if gd is None:
        raise NotFoundError("session not found for evaluation job")

    job.attempts += 1
    job.error = None
    if EvaluationState(job.state) is EvaluationState.FAILED:
        await _set_job_state(session, job, EvaluationState.QUEUED)
    await _set_job_state(session, job, EvaluationState.PREPARING)
    job.started_at = job.started_at or gd.ended_at
    # A retry must not blend old and new agent verdicts (design §39).
    await repo.clear_agent_outputs(session, job.id)

    # 1. Finalise the inputs (design §18): transcript, rolling state, metrics.
    await observer_service.process_pending_chunks(session, gd, up_to_end=True)
    segments = await get_transcript(session, gd.id)
    metrics = await metrics_service.build_metrics(session, gd.id)
    state = await observer_service.current_state(session, gd.id)
    participants = await session_repo.list_active_participants(session, gd.id)
    labels = [p.candidate_label for p in participants]
    names = {p.candidate_label: p.display_name for p in participants}
    user_ids = {p.candidate_label: p.user_id for p in participants}

    if not labels:
        raise ConflictError("session has no participants to evaluate")

    # Tracked turns feed the evidence tools and the interruption penalty; both
    # modes write them the same way.
    tracked_turns = await session_repo.list_turns(session, gd.id)
    interruptions = await session_service.interruption_times(session, gd.id)

    # 2. Orchestration (design §20, §21): plan, dispatch, collect, resolve,
    #    aggregate, synthesise. LangGraph runs this as a state graph; the
    #    `native` engine is the same stages driven by plain asyncio.
    await _set_job_state(session, job, EvaluationState.RUNNING)
    outcome = await _orchestrate(
        gd,
        labels,
        segments,
        metrics,
        state,
        job.rubric,
        attempt=job.attempts,
        evaluation_version=job.evaluation_version,
        turns=[
            TurnRecord(t.candidate_label, t.start, t.end, t.interrupted_label, t.reply_to_label)
            for t in tracked_turns
        ],
        interruptions=interruptions,
    )

    if outcome.failures:
        await _set_job_state(session, job, EvaluationState.WAITING_FOR_AGENT)
    if not any(outcome.outputs_by_candidate.values()):
        raise RuntimeError(
            f"every evaluator agent failed ({len(outcome.failures)} calls); "
            "job cannot aggregate"
        )

    for label, outputs in outcome.outputs_by_candidate.items():
        for output in outputs:
            agent_name = next(
                (r.agent for r in outcome.runs if r.output is output), output.dimension
            )
            await repo.add_agent_output(
                session,
                AgentOutput(
                    job_id=job.id,
                    candidate_label=label,
                    dimension=output.dimension,
                    agent_name=agent_name,
                    score=output.score,
                    confidence=output.confidence,
                    strengths=output.strengths,
                    weaknesses=output.weaknesses,
                    evidence=[e.model_dump() for e in output.evidence],
                    provenance=_provenance(outcome.runs, output),
                ),
            )

    # 3. Deterministic aggregation (design §31). Computed inside the graph by
    #    a node that calls the same aggregator — no model runs in it.
    await _set_job_state(session, job, EvaluationState.AGGREGATING)
    scores = outcome.scores
    for label, score in scores.items():
        await repo.add_score(
            session,
            CandidateScore(
                job_id=job.id,
                candidate_label=label,
                user_id=user_ids.get(label),
                overall=score.overall,
                dimensions=score.dimensions,
                weights=score.weights,
                confidence=score.confidence,
                needs_human_review=score.needs_human_review,
                metrics={
                    **(
                        metrics.per_candidate[label].model_dump()
                        if label in metrics.per_candidate
                        else {}
                    ),
                    "interruption_penalty": score.penalty,
                    "interruption_times": score.interruption_times,
                },
            ),
        )

    # 4. Reports (design §33, §34), each with a synthesis narrative on top.
    await _set_job_state(session, job, EvaluationState.REPORT_GENERATING)

    # The synthesis agent already ran, one branch per candidate. It reads the
    # computed score through tools and writes the narrative; it cannot change a
    # number (claude.md invariant 5).
    syntheses = outcome.syntheses

    for label, score in scores.items():
        report = build_candidate_report(
            candidate_label=label,
            display_name=names.get(label, label),
            score=score,
            outputs=outcome.outputs_by_candidate.get(label, []),
            metrics=metrics.per_candidate[label],
            synthesis=syntheses[label].to_dict() if label in syntheses else {},
        )
        await repo.add_report(
            session,
            Report(
                job_id=job.id,
                session_id=gd.id,
                candidate_label=label,
                user_id=user_ids.get(label),
                body=report.to_dict(),
            ),
        )

    group = build_group_report(str(gd.id), gd.topic, scores, metrics, state)
    body = group.to_dict()
    # The group report agent writes the narrative; every figure in `body` stays computed.
    narrative = await synthesise_group(gd.topic, scores, names, metrics, state, syntheses)
    if narrative:
        body["narrative"] = narrative
    if outcome.conflicts:
        # Surfaced rather than resolved — a human reviewer needs to see it.
        body["agent_conflicts"] = outcome.conflicts
    if outcome.failures:
        body["agent_failures"] = outcome.failures
    await repo.add_report(
        session,
        Report(job_id=job.id, session_id=gd.id, candidate_label=None, user_id=None, body=body),
    )

    # 5. Finish.
    await _set_job_state(session, job, EvaluationState.COMPLETED)
    job.completed_at = gd.ended_at
    if SessionState(gd.state) is SessionState.EVALUATING:
        gd.state = str(transition_session(SessionState.EVALUATING, SessionState.COMPLETED))
    await session.flush()
    await _increment_sessions_completed(session, list(user_ids.values()))

    await publish(
        Event.EVALUATION_COMPLETED,
        str(gd.id),
        {"evaluation_version": job.evaluation_version, "candidates": list(scores)},
    )
    await publish(Event.REPORT_GENERATED, str(gd.id), {"job_id": str(job.id)})
    recipients = [u for u in user_ids.values() if u is not None]
    await notify(
        session,
        recipients,
        NotificationKind.EVALUATION_COMPLETED,
        "Your evaluation is ready",
        f"Scored across {len(next(iter(scores.values())).weights)} dimensions with "
        "supporting evidence from the transcript.",
        session_id=gd.id,
        link=f"/report/{gd.id}",
    )
    await notify(
        session,
        recipients + [gd.host_id],
        NotificationKind.REPORT_AVAILABLE,
        "Report available",
        gd.topic,
        session_id=gd.id,
        link=f"/report/{gd.id}",
    )
    await hub.broadcast(
        str(gd.id),
        server_frame(
            ServerEvent.EVALUATION_COMPLETED,
            str(gd.id),
            {"job_id": str(job.id), "evaluation_version": job.evaluation_version},
        ),
    )
    logger.info(
        "evaluation completed",
        extra={
            "session_id": str(gd.id),
            "job_id": str(job.id),
            "candidates": len(scores),
            "failed_agents": len(outcome.failures),
            "engine": settings.evaluation_engine,
        },
    )
    return job


async def _increment_sessions_completed(session: AsyncSession, user_ids: list) -> None:
    """FR-02: the profile's session count is derived from completed evaluations."""
    from services.auth import repository as auth_repo

    for user_id in {u for u in user_ids if u is not None}:
        profile = await auth_repo.get_profile(session, user_id)
        if profile is not None:
            profile.sessions_completed += 1
    await session.flush()


async def fail_job(session: AsyncSession, job: EvaluationJob, error: str) -> EvaluationJob:
    """Mark a job failed. The session stays EVALUATING so it can be retried."""
    job.error = error[:2000]
    job.state = str(transition_evaluation(EvaluationState(job.state), EvaluationState.FAILED))
    await session.flush()
    logger.error(
        "evaluation job failed",
        extra={"job_id": str(job.id), "session_id": str(job.session_id), "error": error},
    )
    return job


async def run_for_session(session: AsyncSession, session_id: uuid.UUID) -> EvaluationJob:
    """Run the latest job for a session inline.

    Used by the "evaluate now" admin endpoint and by the tests, which need the
    pipeline to run without a Redis-backed worker.
    """
    gd = await session_service.require_session(session, session_id)
    job = await repo.latest_job(session, session_id)
    if job is None:
        job = await request_evaluation(session, gd)
    try:
        return await run_job(session, job)
    except Exception as exc:
        await fail_job(session, job, str(exc))
        raise


def outputs_to_contracts(rows: list[AgentOutput]) -> dict[str, list[EvaluationOutput]]:
    """Stored agent outputs → contract objects, for re-deriving a score."""
    grouped: dict[str, list[EvaluationOutput]] = {}
    for row in rows:
        grouped.setdefault(row.candidate_label, []).append(
            EvaluationOutput(
                candidate_id=row.candidate_label,
                dimension=row.dimension,
                score=row.score,
                confidence=row.confidence,
                strengths=row.strengths,
                weaknesses=row.weaknesses,
                evidence=row.evidence,
            )
        )
    return grouped


async def latest_state(session: AsyncSession, session_id: uuid.UUID) -> RollingState:
    return await observer_service.current_state(session, session_id)
