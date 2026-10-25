"""The evaluation workflow as a LangGraph state graph (design §20, §21).

Purpose:     Wires the whole evaluation pipeline into one LangGraph state graph:
             plan -> (parallel specialists) -> collect -> critic -> optional
             revise -> aggregate -> (parallel synthesis) -> done.
Why needed:  The specialist roster is chosen at runtime, so the number of parallel
             branches isn't known up front; LangGraph's `Send` fan-out and
             reducers handle that without hand-written collection code. It also
             gives an inspectable topology and a path to durable resume.
Contains:
  - `EvaluationState`     the shared state dict flowing through the graph.
  - `plan_node`           pick the roster, build shared context and packets.
  - `fan_out`             one parallel branch per specialist (or per candidate).
  - `evaluate_node`       run one specialist (never raises; failures are values).
  - `_tools`              evidence-lookup tools for specialists, if enabled.
  - `collect_node`        resolve missing / conflicting results via the Supervisor.
  - `critic_node`         audit each verdict's evidence against the transcript.
  - `needs_revision`      route: revise once, or go straight to aggregation.
  - `revise_node`         send flagged verdicts back to their specialist.
  - `aggregate_node`      the deterministic score (no model runs here).
  - `fan_out_synthesis`   one synthesis branch per candidate.
  - `synthesise_node`     the tool-using agent writes one candidate's narrative.
  - `has_any_output`      route: nothing to aggregate means the job failed.
  - `build_graph`, `get_graph`, `render_mermaid`  compile / cache / draw the graph.
  - `run_graph`           run one evaluation and return the final state.
Used by:     evaluation/service.py (the evaluation job runner) and
             tests/test_langgraph_engine.py.
Depends on:  common/config.py, common/contracts.py, evaluation/agents/base.py,
             agents/definitions.py, aggregator.py, evidence.py, supervisor.py,
             synthesis.py, tools.py, and metrics/engine.py.

The pipeline design §20 draws is a graph, and this is that graph expressed
directly:

    prepare ─> plan ─┬─(Send, one per specialist)─> evaluate ─┐
                     └───────────────────────────────────────┴─> collect
                                                                    │
                                                       ┌────────────┘
                                                       ▼
                                        aggregate (deterministic, no model)
                                                       │
                                                       ▼
                                        synthesise (tool-using agent)
                                                       │
                                                       ▼
                                                     report

Why LangGraph rather than a hand-rolled loop:

* **`Send` is dynamic fan-out.** The specialist roster is chosen at runtime from
  the GD type (design §22), so the number of parallel branches is not known
  until `plan` has run. That is precisely what `Send` exists for, and the
  reducer on `runs` merges the branches back without any collection code.
* **Checkpointing is available where a hand-rolled loop has nothing.** Attach a
  persistent checkpointer and a job that dies during `synthesise` resumes there
  instead of re-running every evaluator call (design §38, §39). Not enabled by
  default — see the note on `_compiled` for why an in-process saver would be
  worse than none.
* **The graph is inspectable.** `render_mermaid()` emits the real topology from
  the code, not a diagram drawn by hand that drifts from it.

What LangGraph is deliberately *not* used for: the score. `aggregate` is a node
that calls the same deterministic aggregator as before, and no model runs inside
it (claude.md invariant 5). Framework or not, the number stays in code.
"""

import logging  # node progress logs
import operator  # operator.add merges list results from parallel nodes
from typing import Annotated, Any, TypedDict  # graph state typing with reducers

from langgraph.graph import END, START, StateGraph  # build the state graph
from langgraph.types import Send  # dynamic fan-out to one node run per agent

from services.common.config import settings  # evaluation strategy, agentic flag
from services.common.contracts import EvaluationOutput, RollingState, TranscriptSegment
from services.evaluation.agents.base import AgentRun, run_agent, run_agent_batch  # run a specialist
from services.evaluation.agents.definitions import ALL_AGENTS, select_agents  # agent roster
from services.evaluation.aggregator import AggregatedScore, aggregate_session  # fixed formula
from services.evaluation.evidence import build_packets, build_shared_context  # prompt inputs
from services.evaluation.supervisor import (  # plan resolution and dispatch strategy
    STRATEGY_PER_DIMENSION,
    EvaluationPlanResult,
    resolve,
)
from services.evaluation.synthesis import Synthesis, synthesise  # narrative per candidate
from services.evaluation.tools import TurnRecord, audit_evidence, build_evidence_tools  # tools
from services.metrics.engine import SessionMetrics  # measured numbers

logger = logging.getLogger(__name__)


class EvaluationState(TypedDict, total=False):
    """The graph's state.

    `runs` and `syntheses` carry `operator.add` reducers because parallel
    branches write to them concurrently — that annotation is what makes the
    fan-in work without a collection step.
    """

    # Inputs, set once by the caller.
    session_id: str
    topic: str
    gd_type: str
    duration_seconds: int
    candidates: list[str]
    segments: list[TranscriptSegment]
    metrics: SessionMetrics
    rolling_state: RollingState
    weights: dict[str, float]
    strategy: str
    # Tracked turns and each candidate's interruption times (both modes).
    turns: list[TurnRecord]
    interruptions: dict[str, list[float]]

    # Produced by `plan`.
    agent_names: list[str]
    context: Any
    packets: dict[str, Any]

    # Merged from the parallel evaluate branches.
    runs: Annotated[list[AgentRun], operator.add]
    # Critic verdicts: "agent/candidate" -> problems found (empty = passed).
    critic_notes: dict[str, list[str]]

    # Produced downstream.
    outputs_by_candidate: dict[str, list[EvaluationOutput]]
    failures: list[str]
    conflicts: list[str]
    scores: dict[str, AggregatedScore]
    syntheses: Annotated[list[tuple[str, Synthesis]], operator.add]


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def plan_node(state: EvaluationState) -> dict[str, Any]:
    """Design §21 steps 1–4: choose the roster, build the context and packets."""
    # Which specialists run depends on the type of discussion.
    agents = select_agents(state["gd_type"])
    # The context every candidate and agent shares (topic, Observer's understanding).
    context = build_shared_context(
        session_id=state["session_id"],
        topic=state["topic"],
        gd_type=state["gd_type"],
        duration_seconds=state["duration_seconds"],
        participants=state["candidates"],
        state=state["rolling_state"],
    )
    # One evidence packet per candidate (their lines, metrics, interactions).
    packets = build_packets(state["candidates"], state["segments"], state["metrics"])

    logger.info(
        "graph planned evaluation",
        extra={
            "session_id": state["session_id"],
            "agents": [a.name for a in agents],
            "candidates": list(packets),
            "strategy": state.get("strategy", settings.evaluation_strategy),
        },
    )
    return {
        "agent_names": [a.name for a in agents],
        "context": context,
        "packets": packets,
    }


def fan_out(state: EvaluationState) -> list[Send]:
    """Dynamic fan-out: one branch per specialist, or per (specialist, candidate).

    The roster is only known after `plan`, which is why this is a `Send` list
    computed at runtime rather than a set of static edges.
    """
    strategy = state.get("strategy", settings.evaluation_strategy)
    # Default strategy: one call per specialist covering all candidates (design §49).
    if strategy == STRATEGY_PER_DIMENSION:
        return [
            Send("evaluate", {**state, "agent_name": name, "target": None})
            for name in state["agent_names"]
        ]
    # Comparison strategy: one call per (specialist, candidate) pair.
    return [
        Send("evaluate", {**state, "agent_name": name, "target": label})
        for name in state["agent_names"]
        for label in sorted(state["packets"])
    ]


async def evaluate_node(state: dict[str, Any]) -> dict[str, Any]:
    """One branch: run a specialist and return its runs for the reducer to merge.

    Never raises — `run_agent`/`run_agent_batch` return failed `AgentRun`s, so a
    dead specialist is isolated to its own branch (NFR-04).
    """
    agent = ALL_AGENTS[state["agent_name"]]
    packets = state["packets"]
    target = state.get("target")

    tools = _tools(state)

    # No target means "score everyone in one call"; otherwise just the named candidate.
    if target is None:
        runs = await run_agent_batch(agent, state["context"], packets, tools)
    else:
        runs = [await run_agent(agent, state["context"], packets[target], tools)]
    return {"runs": runs}


def _tools(state: dict[str, Any]):
    """Evidence tools for the specialists, unless agentic evaluation is switched off."""
    if not settings.evaluation_agentic:
        return None
    # Tools let a specialist look up turns, quotes and metrics before scoring.
    return build_evidence_tools(state["segments"], state["metrics"], state.get("turns") or [])


def collect_node(state: EvaluationState) -> dict[str, Any]:
    """Design §21 step 7: resolve missing and conflicting results."""
    agents = [ALL_AGENTS[name] for name in state["agent_names"]]

    class _Plan:
        """`resolve` only reads `packets`; reuse it rather than duplicating the
        conflict-detection logic in two places."""

        def __init__(self) -> None:
            """Expose just the three attributes `resolve` reads from the live state."""
            self.packets = state["packets"]
            self.agents = agents
            self.context = state["context"]

    # Let the Supervisor sort all the runs into usable outputs, failures and conflicts.
    result: EvaluationPlanResult = resolve(_Plan(), state["runs"])
    return {
        "outputs_by_candidate": result.outputs_by_candidate,
        "failures": [f"{f.agent}/{f.candidate_label}: {f.error}" for f in result.failures],
        "conflicts": result.conflicts,
    }


def critic_node(state: EvaluationState) -> dict[str, Any]:
    """Audit every verdict's evidence against the transcript. Deterministic: the
    critic never re-scores, it only says whether a verdict is supported."""
    notes: dict[str, list[str]] = {}
    for run in state["runs"]:
        # Failed runs have nothing to audit.
        if run.output is None:
            continue
        # Check the cited evidence really matches what that candidate said.
        issues = audit_evidence(run.output, state["segments"])
        run.critic = issues
        if issues:
            notes[f"{run.agent}/{run.candidate_label}"] = issues
    return {"critic_notes": notes}


def needs_revision(state: EvaluationState) -> str:
    """One revision round at most: the critic's verdict is only acted on once."""
    if state.get("critic_notes") and settings.evaluation_agentic:
        return "revise"
    return "aggregate"


async def revise_node(state: EvaluationState) -> dict[str, Any]:
    """Send each flagged verdict back to its specialist with the critic's reasons.

    The revised answer replaces the original only if it parses; a failed revision
    keeps the original so the critic can never delete a score.
    """
    tools = _tools(state)
    # Work on copies so the original outputs survive a failed revision.
    outputs = {k: list(v) for k, v in state["outputs_by_candidate"].items()}
    new_runs: list[AgentRun] = []
    for key, issues in state["critic_notes"].items():
        # Keys look like "agent/candidate"; split them back into the two parts.
        agent_name, label = key.split("/", 1)
        agent = ALL_AGENTS[agent_name]
        # Re-ask that specialist about that candidate, including the critic's objections.
        run = await run_agent(
            agent,
            state["context"],
            state["packets"][label],
            tools,
            feedback="; ".join(issues),
        )
        run.critic = [f"revised after critic: {'; '.join(issues)}"]
        # A failed revision keeps the original verdict.
        if run.output is None:
            continue
        # Swap the revised verdict in for the one it replaces (same dimension).
        outputs[label] = [
            run.output if o.dimension == agent.dimension else o for o in outputs.get(label, [])
        ]
        new_runs.append(run)
    return {"outputs_by_candidate": outputs, "runs": new_runs}


def aggregate_node(state: EvaluationState) -> dict[str, Any]:
    """Design §31. A node, but not an agent — no model runs here.

    This is the boundary the whole system is built around: everything before it
    is inference, everything from here is arithmetic (claude.md invariant 5).
    """
    # Hand the validated per-dimension outputs to the fixed-formula aggregator.
    scores = aggregate_session(
        state["outputs_by_candidate"], state["weights"], state.get("interruptions")
    )
    # Anything the critic flagged that revision did not fix goes to a human; the
    # number is untouched, only the review flag is raised.
    for label, outputs in state["outputs_by_candidate"].items():
        left = sum(len(audit_evidence(o, state["segments"])) for o in outputs)
        if left and label in scores:
            scores[label].needs_human_review = True
            scores[label].review_reasons.append(
                f"{left} evidence problem(s) left after critic review"
            )
    return {"scores": scores}


def fan_out_synthesis(state: EvaluationState) -> list[Send]:
    """One synthesis branch per candidate."""
    return [
        Send("synthesise", {**state, "candidate": label}) for label in sorted(state["scores"])
    ]


async def synthesise_node(state: dict[str, Any]) -> dict[str, Any]:
    """The tool-using agent writes one candidate's narrative (design §20)."""
    label = state["candidate"]
    score = state["scores"][label]
    # The narrative explains the already-computed score; it can read it via tools
    # but never change it.
    result = await synthesise(
        label,
        state["outputs_by_candidate"].get(label, []),
        score.weights or state["weights"],
        state["metrics"].per_candidate[label],
        state["topic"],
        state["segments"],
    )
    return {"syntheses": [(label, result)]}


def has_any_output(state: EvaluationState) -> str:
    """Conditional edge: nothing to aggregate means the job failed.

    Returning `END` here is how a total agent failure surfaces — the caller sees
    empty scores and raises, rather than the graph inventing a report.
    """
    if any(state["outputs_by_candidate"].values()):
        return "critic"
    return END


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------


def build_graph(checkpointer: Any | None = None):
    """Compile the evaluation graph.

    Pass a persistent checkpointer to get resume-on-failure: a job then restarts
    from the last completed node instead of re-running every model call
    (design §38). See the note on `_compiled` for why the default is none.
    """
    graph = StateGraph(EvaluationState)

    # Register the steps...
    graph.add_node("plan", plan_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("collect", collect_node)
    graph.add_node("critic", critic_node)
    graph.add_node("revise", revise_node)
    graph.add_node("aggregate", aggregate_node)
    graph.add_node("synthesise", synthesise_node)

    # ...then the order they run in (conditional edges are the branch points).
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", fan_out, ["evaluate"])
    graph.add_edge("evaluate", "collect")
    graph.add_conditional_edges("collect", has_any_output, ["critic", END])
    graph.add_conditional_edges("critic", needs_revision, ["revise", "aggregate"])
    graph.add_edge("revise", "aggregate")
    graph.add_conditional_edges("aggregate", fan_out_synthesis, ["synthesise"])
    graph.add_edge("synthesise", END)

    return graph.compile(checkpointer=checkpointer)


# Compiled once: building the graph is pure structure and does not depend on a
# session, so there is nothing to rebuild per job.
#
# Deliberately compiled WITHOUT a checkpointer. `MemorySaver` lives in this
# process, so it cannot resume across the worker crash it would exist to
# protect against — while `runs` and `syntheses` use `operator.add` reducers,
# which means a resumed thread *appends* to what it already held. That
# combination buys nothing and silently duplicates every candidate's
# assessment. Durable resume is a real feature, but it needs a persistent
# checkpointer (`langgraph-checkpoint-postgres`) pointed at the same Postgres
# the rest of the system uses; `build_graph` takes one when that is wired up.
_compiled = None


def get_graph():
    """The compiled evaluation graph, built once and reused."""
    global _compiled
    # Lazy build: compile on first use, then reuse the same object.
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


def render_mermaid() -> str:
    """The real topology, emitted from the compiled graph.

    Useful in documentation precisely because it cannot drift from the code —
    it is generated from the graph that actually runs.
    """
    return build_graph().get_graph().draw_mermaid()


async def run_graph(
    session_id: str,
    topic: str,
    gd_type: str,
    duration_seconds: int,
    candidates: list[str],
    segments: list[TranscriptSegment],
    metrics: SessionMetrics,
    rolling_state: RollingState,
    weights: dict[str, float],
    strategy: str | None = None,
    attempt: int = 1,
    evaluation_version: int = 1,
    turns: list[TurnRecord] | None = None,
    interruptions: dict[str, list[float]] | None = None,
) -> EvaluationState:
    """Run one evaluation through the graph and return the final state.

    The thread id includes the attempt number, and that matters: `runs` and
    `syntheses` use `operator.add` reducers, so resuming a thread *appends* to
    whatever it already held. Re-running a job on the same thread would return
    every candidate twice and violate the `agent_outputs` unique constraint —
    the checkpointer doing its job, applied to the wrong intent. Each attempt is
    therefore a fresh thread; a resumed attempt keeps its own.

    Note the limit of `MemorySaver`: it lives in this process, so it cannot
    resume across a worker crash. Durable resume needs a persistent
    checkpointer (`langgraph-checkpoint-postgres`) pointed at the same Postgres
    the rest of the system uses.
    """
    # The starting state: all inputs, plus empty accumulators for the reducers.
    initial: EvaluationState = {
        "session_id": session_id,
        "topic": topic,
        "gd_type": gd_type,
        "duration_seconds": duration_seconds,
        "candidates": candidates,
        "segments": segments,
        "metrics": metrics,
        "rolling_state": rolling_state,
        "weights": weights,
        "strategy": strategy or settings.evaluation_strategy,
        "turns": turns or [],
        "interruptions": interruptions or {},
        "runs": [],
        "syntheses": [],
    }
    # The thread id identifies this attempt. It is inert without a
    # checkpointer, and correct the moment one is attached: each attempt is its
    # own thread, so a re-run never appends to a previous run's state.
    config = {
        "configurable": {"thread_id": f"eval:{session_id}:v{evaluation_version}:a{attempt}"}
    }
    return await get_graph().ainvoke(initial, config=config)
