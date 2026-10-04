"""The Supervisor Agent (FR-21, design §20, §21, §22).

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

import asyncio
import logging
from dataclasses import dataclass, field

from services.common.config import settings
from services.common.contracts import EvaluationOutput, RollingState, TranscriptSegment
from services.evaluation.agents.base import (
    AgentRun,
    EvaluatorAgent,
    run_agent,
    run_agent_batch,
)
from services.evaluation.agents.definitions import select_agents
from services.evaluation.evidence import (
    EvidencePacket,
    SharedContext,
    build_packets,
    build_shared_context,
)
from services.metrics.engine import SessionMetrics

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
    agents = select_agents(gd_type)
    context = build_shared_context(
        session_id=session_id,
        topic=topic,
        gd_type=gd_type,
        duration_seconds=duration_seconds,
        participants=candidates,
        state=state,
    )
    packets = build_packets(candidates, segments, metrics)
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
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENT_CALLS)

    if strategy == STRATEGY_PER_DIMENSION:

        async def run_batch(agent: EvaluatorAgent) -> list[AgentRun]:
            async with semaphore:
                return await run_agent_batch(agent, plan.context, plan.packets)

        batches = await asyncio.gather(*(run_batch(agent) for agent in plan.agents))
        return [run for batch in batches for run in batch]

    async def run_one(agent: EvaluatorAgent, packet: EvidencePacket) -> AgentRun:
        async with semaphore:
            return await run_agent(agent, plan.context, packet)

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

    for run in runs:
        if run.ok and run.output is not None:
            outputs_by_candidate.setdefault(run.candidate_label, []).append(run.output)
        else:
            failures.append(run)

    conflicts: list[str] = []
    for label, outputs in outputs_by_candidate.items():
        by_dimension: dict[str, list[float]] = {}
        for output in outputs:
            by_dimension.setdefault(output.dimension, []).append(output.score)
        for dimension, scores in by_dimension.items():
            if len(scores) > 1 and max(scores) - min(scores) > 2.0:
                conflicts.append(
                    f"{label}/{dimension}: agents disagree by "
                    f"{max(scores) - min(scores):.1f} points"
                )

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
    supervisor_plan = plan(
        session_id, topic, gd_type, duration_seconds, candidates, segments, metrics, state
    )
    runs = await dispatch(supervisor_plan, strategy)
    return resolve(supervisor_plan, runs)
