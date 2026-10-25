"""The specialist roster and dynamic selection (design §22, §24–§29).

Purpose:     Declares the six evaluator specialists (content, communication,
             leadership, collaboration, critical thinking, behavior) with the
             instructions and scoring rubric each one gives the model, and says
             which of them run for each type of group discussion.
Why needed:  The rubric text IS the evaluation policy. Keeping it in one data
             file (not scattered through code) lets it be reviewed and versioned,
             and the GD-type table is what makes the pipeline "agentic": the
             Supervisor picks a roster instead of running a fixed script.
Contains:
  - `CONTENT`, `COMMUNICATION`, `LEADERSHIP`, `COLLABORATION`,
    `CRITICAL_THINKING`, `BEHAVIOR`  one `EvaluatorAgent` each (what to judge and
                                      how to score it, including what NOT to reward).
  - `ALL_AGENTS`        name -> agent lookup.
  - `AGENTS_BY_GD_TYPE` which agent names run for technical / leadership /
                        HR-social / general discussions.
  - `select_agents`    turns a GD type into the list of agents to run.
Used by:     evaluation/supervisor.py (plans the roster), evaluation/graph.py,
             evaluation/service.py, and tests (test_evaluation_pipeline.py,
             test_evaluation_batching.py, test_langgraph_engine.py,
             test_llm_providers.py).
Depends on:  evaluation/agents/base.py (`EvaluatorAgent`), session/models.py
             (`GDType`).

Each agent's `focus` and `rubric` are lifted from the design's description of
that agent, including the parts that say what *not* to reward — design §26 is
explicit that "speaking most = leadership" is the wrong model, and the rubric
says so in as many words rather than hoping the model infers it.
"""

from services.evaluation.agents.base import EvaluatorAgent  # the specialist definition type
from services.session.models import GDType  # session types that pick which agents run

CONTENT = EvaluatorAgent(
    name="content",
    dimension="content",
    focus=(
        "You judge the substance of what the candidate said: relevance, "
        "correctness, depth, domain knowledge, and use of evidence (design §24)."
    ),
    rubric="""\
9-10  Consistently relevant, accurate, substantive points. Brings specific
      evidence or concrete examples. Adds dimensions others missed.
7-8   Relevant and mostly accurate. Some depth. Occasional unsupported claim.
5-6   On topic but shallow — restates the obvious, asserts without support.
3-4   Frequently off-topic, vague, or factually loose.
0-2   Little or no substantive contribution.

Penalise confident inaccuracy more than cautious uncertainty. Reward a candidate
who says "I'm not certain, but..." over one who invents a statistic.""",
)

COMMUNICATION = EvaluatorAgent(
    name="communication",
    dimension="communication",
    focus=(
        "You judge how clearly the candidate expressed themselves: clarity, "
        "structure, articulation, conciseness, confidence and logical "
        "expression (design §25). Judge communication quality, NOT audio or "
        "microphone quality — transcription artefacts are not the candidate's "
        "fault and must not lower this score."
    ),
    rubric="""\
9-10  Clear, well-structured, economical. Points land the first time.
7-8   Generally clear with a discernible structure. Some rambling or hedging.
5-6   Understandable but meandering, repetitive, or hard to follow in places.
3-4   Frequently unclear; the listener has to reconstruct the point.
0-2   Largely incoherent or almost silent.

A concise contributor is not automatically a weak communicator. Judge density of
meaning, not volume of words.""",
)

LEADERSHIP = EvaluatorAgent(
    name="leadership",
    dimension="leadership",
    focus=(
        "You judge constructive influence on the discussion (design §26): "
        "initiating useful directions, summarising, resolving disagreement, "
        "moving things forward, drawing in quiet participants."
    ),
    rubric="""\
9-10  Shaped the discussion's direction, summarised at useful moments, resolved
      a disagreement, or brought in an under-participating candidate.
7-8   Some clear initiative — opened a thread, redirected a stall.
5-6   Participated fully but followed rather than led.
3-4   Passive, or attempted to lead by dominating rather than directing.
0-2   No leadership behaviour, or actively obstructive.

SPEAKING THE MOST IS NOT LEADERSHIP. A candidate with high speaking time and no
direction-setting scores LOW here. A candidate who spoke less but asked the
question that unstuck the discussion scores HIGH. Check the measured behaviour
for who dominated the floor and do not reward it by itself.""",
)

COLLABORATION = EvaluatorAgent(
    name="collaboration",
    dimension="collaboration",
    focus=(
        "You judge how the candidate worked with others (design §27): "
        "listening, responding, building on ideas, respecting disagreement, "
        "integrating others' arguments, supporting useful contributions."
    ),
    rubric="""\
9-10  Repeatedly built on others by name, integrated opposing points, disagreed
      without dismissing.
7-8   Responded to others substantively rather than talking past them.
5-6   Mostly delivered their own view; some engagement.
3-4   Largely ignored others; parallel monologue.
0-2   Dismissive, talked over people, or did not engage at all.

Use the interaction graph: a candidate with no incoming or outgoing edges did
not collaborate, whatever they said. A candidate who responded to several
different people engaged broadly.""",
)

CRITICAL_THINKING = EvaluatorAgent(
    name="critical_thinking",
    dimension="critical_thinking",
    focus=(
        "You judge reasoning quality (design §28): identifying assumptions, "
        "weighing trade-offs, constructing counterarguments, cause-and-effect "
        "reasoning, and synthesis of competing ideas."
    ),
    rubric="""\
9-10  Surfaced an unstated assumption, weighed a real trade-off, or synthesised
      two opposing positions into something better than either.
7-8   Reasoned in steps, acknowledged a counterargument.
5-6   Asserted positions with thin reasoning.
3-4   Reasoning is circular, contradictory, or absent.
0-2   No discernible reasoning.

Distinguish reasoning from vocabulary. A plainly worded argument that identifies
the actual trade-off beats a sophisticated-sounding one that does not.""",
)

BEHAVIOR = EvaluatorAgent(
    name="behavior",
    dimension="behavior",
    focus=(
        "You judge discussion conduct (design §29): dominance, participation "
        "balance, interruptions, turn-taking, engagement and etiquette. Your "
        "primary evidence is the measured behaviour block — it was computed "
        "deterministically from speech events."
    ),
    rubric="""\
9-10  Balanced participation, clean turn-taking, few or no interruptions,
      engaged throughout.
7-8   Broadly good conduct; an occasional interruption or quiet stretch.
5-6   Noticeably unbalanced — dominated the floor, or contributed rarely.
3-4   Repeatedly interrupted others, or was largely disengaged.
0-2   Disruptive, or effectively absent.

Interpret the numbers in context. In a six-person GD an even share is about 17%;
40% is dominance and 3% is disengagement. Interruptions are counted as genuine
overlapping speech, not brief hand-offs. Low participation is a conduct
observation, not a punishment for being concise — check whether the candidate
was talked over before penalising them for silence.""",
    metrics_led=True,
)

# Name -> agent lookup used when the roster table below refers to agents by name.
ALL_AGENTS: dict[str, EvaluatorAgent] = {
    agent.name: agent
    for agent in (CONTENT, COMMUNICATION, LEADERSHIP, COLLABORATION, CRITICAL_THINKING, BEHAVIOR)
}

# Design §22 — the Supervisor picks the roster from the GD type. This mapping is
# what makes the pipeline agentic rather than a fixed script.
AGENTS_BY_GD_TYPE: dict[str, tuple[str, ...]] = {
    GDType.TECHNICAL: (
        "content",
        "critical_thinking",
        "communication",
        "collaboration",
        "behavior",
    ),
    GDType.LEADERSHIP: ("leadership", "communication", "collaboration", "behavior"),
    GDType.HR_SOCIAL: (
        "communication",
        "collaboration",
        "leadership",
        "behavior",
        "critical_thinking",
    ),
    GDType.GENERAL: (
        "content",
        "communication",
        "leadership",
        "collaboration",
        "behavior",
    ),
}


def select_agents(gd_type: str) -> list[EvaluatorAgent]:
    """Which specialists to run for this GD (design §22)."""
    # Unknown types fall back to the general roster rather than failing the job.
    names = AGENTS_BY_GD_TYPE.get(gd_type, AGENTS_BY_GD_TYPE[GDType.GENERAL])
    # Resolve names to the agent objects the dispatcher needs.
    return [ALL_AGENTS[name] for name in names]
