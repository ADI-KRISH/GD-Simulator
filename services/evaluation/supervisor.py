"""The Supervisor Agent (FR-21, design §20, §21, §22).

Purpose:     Orchestrates one evaluation pass: picks the specialists for this
             discussion, builds the shared context and evidence packets, runs the
             specialists concurrently, then sorts their results into usable
             outputs, failures and score conflicts.
Why needed:  Someone has to coordinate the specialists, isolate failures so one
             bad model call can't sink the whole job, and hand clean, validated
             evidence to the deterministic aggregator. The Supervisor does that
             and deliberately never scores anything itself.
Contains:
  - `MAX_CONCURRENT_AGENT_CALLS`  cap on in-flight model calls (rate limits).
  - `STRATEGY_PER_DIMENSION`, `STRATEGY_PER_CANDIDATE`  the two dispatch modes.
  - `SupervisorPlan`        steps 1-4 result (agents, context, packets) with
                            `call_count` to predict cost.
  - `EvaluationPlanResult`  steps 5-8 result (runs, outputs, failures, conflicts).
  - `plan`                  design §21 steps 1-4.
  - `dispatch`              steps 5-6: run specialists concurrently.
  - `resolve`               step 7: separate good runs from failures and flag
                            large disagreements between agents.
  - `run`                   the whole pass, plan -> dispatch -> resolve.
Used by:     evaluation/graph.py (uses `resolve`, `STRATEGY_PER_DIMENSION`),
             evaluation/service.py, tests/test_evaluation_batching.py and
             tests/test_evaluation_pipeline.py.
Depends on:  common/config.py, common/contracts.py, evaluation/agents/base.py,
             agents/definitions.py, evaluation/evidence.py, metrics/engine.py.

Design §21 lists eight responsibilities, and the Supervisor here does all eight
and nothing more:

1. understand the task        → `plan`
2. determine dimensions       → `select_agents` (design §22, from GD type)
3. construct context          → `build_shared_context`
4. generate evidence packets  → `build_packets`
5. invoke evaluators          → `dispatch`
6. collect outputs            → `dispatch` returns every run, failures included
7. resolve missing/conflicts  → `resolve`
8. hand validated evidence on → `EvaluationPlanResult.outputs_by_candidate`

Note what is absent: the Supervisor never scores. It orchestrates, then hands
validated evidence to the deterministic aggregator (claude.md invariant 5,
design §21).

Orchestration is hand-rolled asyncio rather than a graph framework. The topology
is a fixed fan-out/fan-in — select agents, run them concurrently, collect — and
a dependency that adds a DSL over `asyncio.gather` would obscure that rather
than clarify it. The decision is recorded in claude.md's TODO list.
"""

import asyncio  # run agents concurrently under a semaphore
import logging  # dispatch logs
from dataclasses import dataclass, field  # plan/result containers

from services.common.config import settings  # evaluation strategy and concurrency
from services.common.contracts import EvaluationOutput, RollingState, TranscriptSegment
from services.evaluation.agents.base import (  # specialist runner
    AgentRun,
    EvaluatorAgent,
    run_agent,
    run_agent_batch,
)
from services.evaluation.agents.definitions import select_agents  # choose agents by GD type
from services.evaluation.evidence import (  # prompt inputs
    EvidencePacket,
    SharedContext,
    build_packets,
    build_shared_context,
)
from services.metrics.engine import SessionMetrics  # measured numbers

logger = logging.getLogger(__name__)

# How many agent calls to have in flight at once. Unbounded concurrency would
# hit provider rate limits and make the retry backoff meaningless.
MAX_CONCURRENT_AGENT_CALLS = 5

# Two dispatch strategies, switchable so the same session can be run both ways
# and compared.
#
#   per_dimension  — one call per specialist, covering every candidate.
#                    5 calls for a 6-person technical GD. The shared context
#                    crosses the wire once per specialist instead of once per
#                    (specialist, candidate) pair (design §49), and comparative
#                    dimensions like leadership can actually compare.
#
#   per_candidate  — one call per (specialist, candidate). 30 calls for the same
#                    GD. More expensive, but each response is small and the
#                    schema can pin `candidate_id` to a single value, so a
#                    misattributed score is structurally impossible.
STRATEGY_PER_DIMENSION = "per_dimension"
STRATEGY_PER_CANDIDATE = "per_candidate"


@dataclass(slots=True)
class SupervisorPlan:
    """The result of steps 1–4."""

    gd_type: str
    agents: list[EvaluatorAgent]
    context: SharedContext
    packets: dict[str, EvidencePacket]

    def call_count(self, strategy: str | None = None) -> int:
        """How many model calls this plan will make."""
        # One call per specialist when batched; one per (specialist, candidate) otherwise.
        if (strategy or settings.evaluation_strategy) == STRATEGY_PER_DIMENSION:
            return len(self.agents)
        return len(self.agents) * len(self.packets)


@dataclass(slots=True)
class EvaluationPlanResult:
    """The result of steps 5–8."""

    plan: SupervisorPlan
    runs: list[AgentRun]
    outputs_by_candidate: dict[str, list[EvaluationOutput]] = field(default_factory=dict)
    failures: list[AgentRun] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def plan(
    session_id: str,
    topic: str,
    gd_type: str,
    duration_seconds: int,
    candidates: list[str],
    segments: list[TranscriptSegment],
    metrics: SessionMetrics,
    state: RollingState,
) -> SupervisorPlan:
    """Design §21 steps 1–4."""
    # Which specialists to run depends on the GD type (design §22).
    agents = select_agents(gd_type)
    # Context shared by everyone, then one evidence packet per candidate.
    context = build_shared_context(
        session_id=session_id,
        topic=topic,
        gd_type=gd_type,
        duration_seconds=duration_seconds,
        participants=candidates,
        state=state,
    )
    packets = build_packets(candidates, segments, metrics)
    # Log the plan, including how many model calls it will cost.
    logger.info(
        "supervisor planned evaluation",
        extra={
            "session_id": session_id,
            "gd_type": gd_type,
            "agents": [a.name for a in agents],
            "candidates": list(packets),
            "strategy": settings.evaluation_strategy,
            "calls": len(agents)
            if settings.evaluation_strategy == STRATEGY_PER_DIMENSION
            else len(agents) * len(packets),
        },
    )
    return SupervisorPlan(gd_type=gd_type, agents=agents, context=context, packets=packets)


async def dispatch(plan: SupervisorPlan, strategy: str | None = None) -> list[AgentRun]:
    """Design §21 steps 5–6.

    Calls run concurrently under a semaphore, and a failure is always returned
    as a failed `AgentRun` rather than raised — so one bad call cannot abort the
    others (NFR-04 partial-failure isolation). Both strategies return the same
    shape, one run per (agent, candidate), so everything downstream is unaware
    of which was used.
    """
    strategy = strategy or settings.evaluation_strategy
    # The semaphore caps in-flight model calls so we stay under provider rate limits.
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENT_CALLS)

    # Batched strategy: one call per specialist covering every candidate.
    if strategy == STRATEGY_PER_DIMENSION:

        async def run_batch(agent: EvaluatorAgent) -> list[AgentRun]:
            """One call covering every candidate for this specialist (per-dimension strategy)."""
            async with semaphore:
                return await run_agent_batch(agent, plan.context, plan.packets)

        # Run all specialists at once, then flatten into a single list of runs.
        batches = await asyncio.gather(*(run_batch(agent) for agent in plan.agents))
        return [run for batch in batches for run in batch]

    async def run_one(agent: EvaluatorAgent, packet: EvidencePacket) -> AgentRun:
        """One call for a single (agent, candidate) pair (per-candidate strategy)."""
        async with semaphore:
            return await run_agent(agent, plan.context, packet)

    # Per-candidate strategy: one task per (specialist, candidate) pair.
    tasks = [run_one(agent, packet) for packet in plan.packets.values() for agent in plan.agents]
    return list(await asyncio.gather(*tasks))


def resolve(plan: SupervisorPlan, runs: list[AgentRun]) -> EvaluationPlanResult:
    """Design §21 step 7: resolve missing and conflicting results.

    "Conflict" here means two agents scoring the same candidate on the same
    dimension far apart. The Supervisor records it rather than picking a winner
    — the aggregator averages, and a recorded conflict is what a human reviewer
    needs to see (design §52).
    """
    outputs_by_candidate: dict[str, list[EvaluationOutput]] = {label: [] for label in plan.packets}
    failures: list[AgentRun] = []

    # Sort runs into usable outputs (grouped by candidate) and failures.
    for run in runs:
        if run.ok and run.output is not None:
            outputs_by_candidate.setdefault(run.candidate_label, []).append(run.output)
        else:
            failures.append(run)

    # Look for two agents scoring the same dimension of the same candidate far apart.
    conflicts: list[str] = []
    for label, outputs in outputs_by_candidate.items():
        by_dimension: dict[str, list[float]] = {}
        for output in outputs:
            by_dimension.setdefault(output.dimension, []).append(output.score)
        for dimension, scores in by_dimension.items():
            # More than 2 points apart counts as a conflict worth recording.
            if len(scores) > 1 and max(scores) - min(scores) > 2.0:
                conflicts.append(
                    f"{label}/{dimension}: agents disagree by "
                    f"{max(scores) - min(scores):.1f} points"
                )

    # Surface failures in the logs (first 5 in detail) without raising.
    if failures:
        logger.warning(
            "some evaluator agents failed",
            extra={
                "failed": len(failures),
                "total": len(runs),
                "detail": [f"{f.agent}/{f.candidate_label}: {f.error}" for f in failures][:5],
            },
        )

    return EvaluationPlanResult(
        plan=plan,
        runs=runs,
        outputs_by_candidate=outputs_by_candidate,
        failures=failures,
        conflicts=conflicts,
    )


async def run(
    session_id: str,
    topic: str,
    gd_type: str,
    duration_seconds: int,
    candidates: list[str],
    segments: list[TranscriptSegment],
    metrics: SessionMetrics,
    state: RollingState,
    strategy: str | None = None,
) -> EvaluationPlanResult:
    """The whole Supervisor pass, steps 1–8."""
    # plan -> dispatch -> resolve, in the order design §21 prescribes.
    supervisor_plan = plan(
        session_id, topic, gd_type, duration_seconds, candidates, segments, metrics, state
    )
    runs = await dispatch(supervisor_plan, strategy)
    return resolve(supervisor_plan, runs)
