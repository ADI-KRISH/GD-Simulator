"""Evaluation pipeline (FR-20 – FR-30, design §18–§21, §31, §33).

Purpose:     The orchestrator of a whole evaluation. It creates the durable job
             when a session ends, then (in a worker) loads the transcript,
             metrics and Observer state, runs the agents, stores verdicts and
             scores, builds reports, notifies users and tells the platform.
Why needed:  Every other evaluation module does one narrow thing (agents, the
             aggregator, reports, the queue). This is the one place that wires
             them together in the right order, walks the evaluation state
             machine, and keeps a retried job from producing duplicate or
             contradictory results (design §39).
Contains:
  - `ENGINE_LANGGRAPH`, `ENGINE_NATIVE`  the two interchangeable engines.
  - `OrchestrationOutcome`   what either engine returns, so `run_job` is
                             engine-agnostic.
  - `eval_topic`             the topic as evaluators see it (adds practice-mode context).
  - `_orchestrate`           run the workflow on the configured engine.
  - `_provenance`            tool calls + critic notes saved with each verdict.
  - `_set_job_state`         move a job through the legal state transitions.
  - `to_contract`            job row -> the `evaluation_job` contract on the queue.
  - `request_evaluation`     create + enqueue a job (cheap, idempotent).
  - `run_job`                execute one job end to end (the main function).
  - `_platform_subjects`, `_increment_sessions_completed`  post-completion bookkeeping.
  - `fail_job`               mark a job failed (session stays retryable).
  - `run_for_session`        run a session's job inline (admin endpoint, tests).
  - `outputs_to_contracts`   stored verdicts -> contract objects (re-derive a score).
  - `latest_state`           the Observer's most recent rolling state.
Used by:     evaluation/router.py, evaluation/worker.py and several tests
             (test_evaluation_pipeline.py, test_notifications.py,
             test_session_lifecycle.py).
Depends on:  nearly every evaluation module (graph, supervisor, aggregator,
             report, synthesis, group_synthesis, queue, repository, models), plus
             metrics, observer, speech, session, notifications, realtime,
             auth and ai_platform for inputs and side effects.

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

import asyncio  # run work concurrently; cancellation handling
import logging  # job lifecycle logs
import uuid  # session/job id types
from dataclasses import dataclass  # small result containers

from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.ai_platform import skill_graph as platform_skill_graph  # send results to M04
from services.ai_platform.bridge import platform_subject  # map a GD account to a platform student
from services.auth import repository as auth_repo  # load participants' user accounts
from services.common.config import settings  # engine, versions, penalty settings
from services.common.contracts import (  # validated data shapes
    EvaluationArtifacts,
    EvaluationOutput,
    EvaluationVersions,
    RollingState,
    validate_contract,
)
from services.common.contracts import (
    EvaluationJob as EvaluationJobContract,
)
from services.common.errors import ConflictError, NotFoundError  # 409 / 404
from services.common.events import Event, publish  # announce evaluation.* events
from services.common.storage import get_object_store  # delete raw audio after evaluation
from services.evaluation import queue  # enqueue jobs for the worker
from services.evaluation import repository as repo  # job/score/report data access
from services.evaluation.agents.definitions import select_agents  # agents for the GD type
from services.evaluation.aggregator import aggregate_session, normalise_weights  # fixed formula
from services.evaluation.graph import run_graph  # LangGraph engine
from services.evaluation.group_synthesis import synthesise_group  # group-level narrative
from services.evaluation.models import AgentOutput, CandidateScore, EvaluationJob, Report  # tables
from services.evaluation.progress import progress  # change vs earlier sessions
from services.evaluation.report import (  # assemble report objects
    build_candidate_report,
    build_group_report,
    entry_moments,
)
from services.evaluation.supervisor import run as run_supervisor  # native (asyncio) engine
from services.evaluation.synthesis import synthesise  # per-candidate narrative
from services.evaluation.tools import TurnRecord  # turn data for evidence tools
from services.metrics import service as metrics_service  # deterministic metrics
from services.notifications.models import NotificationKind  # notification types
from services.notifications.service import notify  # tell users their report is ready
from services.observability.recorder import chain  # group a run's calls under one parent
from services.observer import service as observer_service  # final rolling state
from services.realtime.hub import hub  # push evaluation completion to the room
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.session import repository as session_repo  # session/participant lookups
from services.session import service as session_service  # session access and state
from services.session.models import GDSession  # session row type
from services.session.state import (  # state machines
    EvaluationState,
    SessionState,
    transition_evaluation,
    transition_session,
)
from services.speech import repository as speech_repo  # audio chunk rows
from services.speech.service import get_transcript  # the raw transcript

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
    # Group sessions: the topic is used unchanged.
    if gd.mode != "solo":
        return gd.topic
    # Practice sessions: append a note so evaluators know the other voices are AI.
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
    # Default engine: the LangGraph state graph.
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
        # Normalise the graph's final state into the shared outcome shape.
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
    # Same deterministic aggregator as the graph path, so both engines score identically.
    scores = aggregate_session(result.outputs_by_candidate, weights, interruptions)
    # Write each candidate's narrative concurrently, paired back to its label.
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
    # Find the run that produced this exact output object, to read its audit trail.
    run = next((r for r in runs if r.output is output), None)
    return {"tool_calls": run.tool_calls, "critic": run.critic} if run else {}


async def _set_job_state(
    session: AsyncSession, job: EvaluationJob, target: EvaluationState
) -> EvaluationJob:
    """Move a job to `target` through the evaluation transition table."""
    # `transition_evaluation` raises on an illegal move, so states can't skip around.
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
    # Fail early if the message wouldn't satisfy the published contract.
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
    # A session still in progress can't be evaluated.
    if SessionState(gd.state) not in {
        SessionState.ENDED,
        SessionState.EVALUATING,
        SessionState.COMPLETED,
        SessionState.FAILED,
    }:
        raise ConflictError("a session must end before it can be evaluated")

    # Reuse the existing job unless a new version was asked for (or it failed).
    if not force_new_version:
        existing = await repo.latest_job(session, gd.id)
        if existing is not None and EvaluationState(existing.state) not in {EvaluationState.FAILED}:
            # Idempotent: asking twice returns the same job (design §39).
            return existing

    # New job: freeze the rubric (normalised) and the version triple so the run is reproducible.
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

    # The session itself moves ENDED -> EVALUATING now that a job exists.
    if SessionState(gd.state) is SessionState.ENDED:
        gd.state = str(transition_session(SessionState.ENDED, SessionState.EVALUATING))
        await session.flush()

    # Hand the job to the queue; the durable row above is the safety net if this is lost.
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

    # Start (or restart) the attempt: count it, clear the last error, enter PREPARING.
    job.attempts += 1
    job.error = None
    # A previously failed job must pass through QUEUED to stay within the transition table.
    if EvaluationState(job.state) is EvaluationState.FAILED:
        await _set_job_state(session, job, EvaluationState.QUEUED)
    await _set_job_state(session, job, EvaluationState.PREPARING)
    job.started_at = job.started_at or gd.ended_at
    # A retry must not blend old and new agent verdicts (design §39).
    await repo.clear_agent_outputs(session, job.id)
    # No write transaction may stay open across model calls: on SQLite it locks
    # out every other writer for as long as the evaluation runs, which on a
    # rate-limited free tier is minutes. So the job's progress is committed at
    # each step that precedes a model call — which also lets a student see the
    # job as preparing or running instead of a 404.
    await session.commit()

    # 1. Finalise the inputs (design §18): transcript, rolling state, metrics.
    # First let the Observer finish any audio chunks it hadn't processed yet.
    await observer_service.process_pending_chunks(session, gd, up_to_end=True)
    segments = await get_transcript(session, gd.id)
    metrics = await metrics_service.build_metrics(session, gd.id)
    state = await observer_service.current_state(session, gd.id)
    # Who took part: labels (A, B, ...), display names, and linked user accounts.
    participants = await session_repo.list_active_participants(session, gd.id)
    labels = [p.candidate_label for p in participants]
    names = {p.candidate_label: p.display_name for p in participants}
    # Who said each line, by name — the AI personas too, for the entry moments.
    speaker_names = {
        **{p["label"]: p["name"] for p in (gd.personas or []) if "label" in p},
        **names,
    }
    user_ids = {p.candidate_label: p.user_id for p in participants}

    # Nothing to score if nobody took part.
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
    await session.commit()
    # Every model call from here to the group report is tagged with the session and run.
    run_tag = f"eval v{job.evaluation_version} a{job.attempts}"
    async with chain(
        f"evaluation {run_tag}", purpose="evaluation", session_id=str(gd.id), run=run_tag
    ):
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
                TurnRecord(
                    t.candidate_label, t.start, t.end, t.interrupted_label, t.reply_to_label
                )
                for t in tracked_turns
            ],
            interruptions=interruptions,
        )

    # Some agents failed: record that the job is degraded but continue with the rest.
    if outcome.failures:
        await _set_job_state(session, job, EvaluationState.WAITING_FOR_AGENT)
    # If every agent failed there is nothing to aggregate; raise so the worker retries.
    if not any(outcome.outputs_by_candidate.values()):
        raise RuntimeError(
            f"every evaluator agent failed ({len(outcome.failures)} calls); "
            "job cannot aggregate"
        )

    # Persist each validated agent verdict exactly as produced (the evidence trail).
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
    # Store each candidate's final score with the weights and metrics behind it.
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

    # One report per candidate: scores + evidence + feedback + narrative + entry moments.
    for label, score in scores.items():
        report = build_candidate_report(
            candidate_label=label,
            display_name=names.get(label, label),
            score=score,
            outputs=outcome.outputs_by_candidate.get(label, []),
            metrics=metrics.per_candidate[label],
            synthesis=syntheses[label].to_dict() if label in syntheses else {},
            moments=entry_moments(segments, label, names.get(label, label), speaker_names),
        )
        # Past scores only coach: shown beside the report, never fed to an evaluator.
        if user_ids.get(label):
            past = await repo.list_scores_for_user(session, user_ids[label])
            report.progress = progress(
                score.dimensions, [p.dimensions for p in past if p.job_id != job.id]
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

    # Then the group-level report (candidate_label=None marks it as the group's).
    group = build_group_report(str(gd.id), gd.topic, scores, metrics, state)
    body = group.to_dict()
    # The group report agent writes the narrative; every figure in `body` stays computed.
    async with chain(
        "group report", purpose="group_report", session_id=str(gd.id), run=run_tag
    ):
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
    # Mark the job and the session COMPLETED and bump each user's completed count.
    await _set_job_state(session, job, EvaluationState.COMPLETED)
    job.completed_at = gd.ended_at
    if SessionState(gd.state) is SessionState.EVALUATING:
        gd.state = str(transition_session(SessionState.EVALUATING, SessionState.COMPLETED))
    await session.flush()
    await _increment_sessions_completed(session, list(user_ids.values()))
    if settings.delete_audio_after_evaluation:
        await purge_audio(session, gd.id)

    # Tell the rest of the system: event bus, in-app notifications, then the live room.
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
    # The platform's skill graph (M04) hears about it: after a commit, so no
    # write transaction is held across the calls, and never at the report's cost.
    if settings.platform_bridge:
        subjects = await _platform_subjects(session, user_ids)
        await session.commit()
        await platform_skill_graph.send_results(str(gd.id), scores, segments, subjects)
    return job


async def purge_audio(session: AsyncSession, session_id: uuid.UUID) -> None:
    """Delete a finished session's raw audio (files and rows); best effort.

    The transcript already holds what was said, so the recording is no longer
    needed. A storage hiccup must never fail a completed evaluation.
    """
    store = get_object_store()
    for chunk in await speech_repo.list_audio_chunks(session, session_id):
        try:
            await store.delete(chunk.storage_key)
        except Exception:
            logger.warning("audio delete failed", extra={"key": chunk.storage_key}, exc_info=True)
            continue
        await session.delete(chunk)
    await session.flush()


async def _platform_subjects(
    session: AsyncSession, user_ids: dict[str, uuid.UUID | None]
) -> dict[str, str]:
    """Candidate label -> platform subject, for candidates who came from the platform."""
    subjects: dict[str, str] = {}
    for label, user_id in user_ids.items():
        user = await auth_repo.get_user_by_id(session, user_id) if user_id else None
        # Only accounts created through the platform bridge map to a platform subject.
        subject = platform_subject(user.email) if user else None
        if subject:
            subjects[label] = subject
    return subjects


async def _increment_sessions_completed(session: AsyncSession, user_ids: list) -> None:
    """FR-02: the profile's session count is derived from completed evaluations."""
    from services.auth import repository as auth_repo

    # De-duplicated, skipping candidates with no linked account.
    for user_id in {u for u in user_ids if u is not None}:
        profile = await auth_repo.get_profile(session, user_id)
        if profile is not None:
            profile.sessions_completed += 1
    await session.flush()


async def fail_job(session: AsyncSession, job: EvaluationJob, error: str) -> EvaluationJob:
    """Mark a job failed. The session stays EVALUATING so it can be retried."""
    # Keep the error message bounded so it fits the column and the API response.
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
    # Create the job on demand if the session doesn't have one yet.
    job = await repo.latest_job(session, session_id)
    if job is None:
        job = await request_evaluation(session, gd)
    try:
        return await run_job(session, job)
    except Exception as exc:
        # Record the failure on the job, then re-raise so the caller sees it.
        await fail_job(session, job, str(exc))
        raise


def outputs_to_contracts(rows: list[AgentOutput]) -> dict[str, list[EvaluationOutput]]:
    """Stored agent outputs → contract objects, for re-deriving a score."""
    grouped: dict[str, list[EvaluationOutput]] = {}
    # Rebuild contract objects from DB rows and group them by candidate.
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
    """The Observer's most recent rolling state for the session."""
    return await observer_service.current_state(session, session_id)
