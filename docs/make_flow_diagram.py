"""Draws docs/evaluation_flow.png: the agentic evaluation flow and its submodules.

Run with a Python that has matplotlib:  python docs/make_flow_diagram.py
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).with_name("evaluation_flow.png")

BG = "#0b0e17"
INK = "#eef0f8"
MUTED = "#9aa3bd"
KINDS = {  # fill, edge
    "llm": ("#2a2159", "#9b8cff"),  # LLM agent
    "code": ("#0f3a3a", "#3fd0b4"),  # deterministic code
    "store": ("#232838", "#7d86a3"),  # data store
    "ext": ("#3f2e0f", "#ffb347"),  # external service / media
    "tool": ("#3a1f2e", "#ff7aa8"),  # tool the agents call
}

fig, ax = plt.subplots(figsize=(28, 19), dpi=110)
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.set_xlim(0, 280)
ax.set_ylim(0, 190)
ax.axis("off")


def box(x, y, w, h, text, kind="code", size=10.5, bold=False):
    fill, edge = KINDS[kind]
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.4,rounding_size=2",
            fc=fill,
            ec=edge,
            lw=1.8,
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        color=INK,
        fontsize=size,
        fontweight="bold" if bold else "normal",
        linespacing=1.35,
    )
    return (x, y, w, h)


def lane(x, y, w, h, title, tx=None, ha="left"):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.4,rounding_size=3",
            fc="#10141f",
            ec="#2b3247",
            lw=1.4,
            ls="--",
        )
    )
    ax.text(
        tx if tx is not None else x + 2.5,
        y + h - 3.2,
        title,
        color=MUTED,
        fontsize=13,
        fontweight="bold",
        va="center",
        ha=ha,
    )


def arrow(a, b, label="", color="#c9cff0", style="-|>", ls="-", rad=0.0, lw=1.8, off=(0, 1.6)):
    ax.add_patch(
        FancyArrowPatch(
            a,
            b,
            arrowstyle=style,
            mutation_scale=16,
            color=color,
            lw=lw,
            ls=ls,
            connectionstyle=f"arc3,rad={rad}",
        )
    )
    if label:
        ax.text(
            (a[0] + b[0]) / 2 + off[0],
            (a[1] + b[1]) / 2 + off[1],
            label,
            color=color,
            fontsize=9.5,
            ha="center",
            va="bottom",
        )


def right(b):
    return (b[0] + b[2], b[1] + b[3] / 2)


def left(b):
    return (b[0], b[1] + b[3] / 2)


def top(b):
    return (b[0] + b[2] / 2, b[1] + b[3])


def bottom(b):
    return (b[0] + b[2] / 2, b[1])


# ------------------------------------------------------------------ title
ax.text(
    140,
    185,
    "GD Platform: agentic evaluation flow",
    color=INK,
    fontsize=26,
    fontweight="bold",
    ha="center",
)
ax.text(
    140,
    180.5,
    "Live session on top, asynchronous evaluation below. The score itself is always computed in code.",
    color=MUTED,
    fontsize=13,
    ha="center",
)

# ----------------------------------------------------------- 1. live lane
lane(3, 118, 274, 58, "1  LIVE SESSION  (decoupled: an AI failure never stops the discussion)")

mic = box(7, 147, 36, 15, "Browser mic + camera\nLiveKit audio/video", "ext")
ctl = box(7, 124, 36, 15, "Control WebSocket\nspeech started / stopped", "ext")
stt = box(52, 147, 38, 15, "Speech-to-text (45 s windows)\nWhisper  |  ElevenLabs Scribe", "ext")
seg = box(99, 147, 36, 15, "Transcript segments\nsaved the moment they exist", "store")
obs = box(144, 147, 40, 15, "OBSERVER AGENT\nrolling state: arguments, positions\nobservations only, no scores", "llm", bold=True)
roll = box(144, 124, 40, 15, "Rolling state\n(compressed understanding)", "store")
trk = box(52, 124, 38, 15, "Turn tracker\nwho interrupted / replied to whom", "code")
turns = box(99, 124, 36, 15, "turns table\ninterruptions, replies, times", "store")

arrow(right(mic), left(stt))
arrow(right(stt), left(seg))
arrow(right(seg), left(obs))
arrow(bottom(obs), top(roll))
arrow(bottom(mic), top(ctl))
arrow(right(ctl), left(trk))
arrow(right(trk), left(turns))

# Practice mode group (right side of the live lane)
ax.add_patch(
    FancyBboxPatch((193, 122), 80, 49, boxstyle="round,pad=0.4,rounding_size=3", fc="#12182a", ec="#3fd0b4", lw=1.6)
)
ax.text(233, 168, "PRACTICE MODE (solo, fully spoken)", color="#3fd0b4", fontsize=12, fontweight="bold", ha="center")
vad = box(197, 148, 34, 14, "Student voice\nbrowser VAD → utterance WAV", "ext", size=9.5)
pstt = box(237, 148, 32, 14, "Scribe / Whisper\nSTT → text", "ext", size=9.5)
pers = box(197, 127, 34, 16, "PERSONA AGENTS\nArjun, Bhavana, Chitra, Dev,\nEsha, Farhan (LLM)", "llm", size=9.5, bold=True)
tts = box(237, 127, 32, 16, "ElevenLabs TTS\nturbo v2.5 voices", "ext", size=9.5)
arrow(right(vad), left(pstt), lw=1.5)
arrow(bottom(pstt), top(tts), lw=1.5, rad=-0.0)
arrow(left(tts), right(pers), lw=1.5)
arrow(bottom(vad), top(pers), "barge-in = interruption", color="#ff6b6b", lw=1.5, off=(14, 0.4))
# practice writes the same tables as multiplayer
ax.text(233, 124.6, "writes the same transcript, turns and speech events", color="#3fd0b4", fontsize=9.2, ha="center", va="center")

# --------------------------------------------------------- 2. job + inputs
lane(3, 84, 274, 31, "2  SESSION ENDS  →  EVALUATION JOB  (idempotent per session + version, durable queue)")

end = box(7, 90, 36, 15, "Session ENDED\n(timer or host)", "code")
job = box(52, 90, 38, 15, "Evaluation job\nQUEUED → RUNNING", "code")
prep = box(99, 90, 70, 15, "Prepare inputs: transcript + rolling state + DETERMINISTIC METRICS\nspeaking time, turns, interruptions, response latency, interaction graph\n+ tracked turns + interruption times", "code", size=10)
note = box(178, 90, 95, 15, "Idempotent: the same session + version never creates a second report.\nOnly the human candidates are scored. In practice mode the AI personas\nare tracked as speakers but are not evaluated.", "store", size=10)
arrow(right(end), left(job))
arrow(right(job), left(prep))
# inputs come from the live lane
arrow((134, 118.4), (134, 105.6), "reads transcript, turns and rolling state from the database", color="#7d86a3", ls="--", lw=1.6, off=(30, -1.5))

# ------------------------------------------------------- 3. LangGraph lane
lane(3, 3, 274, 77, "3  LANGGRAPH PIPELINE  (services/evaluation/graph.py)", tx=200, ha="center")

plan = box(7, 40, 28, 18, "PLAN\nchoose roster by\nGD type, build\nevidence packets", "code", bold=True)
ax.plot([134, 134, 21], [89.6, 82, 82], color="#c9cff0", lw=1.8)
arrow((21, 82), (21, 58.6), color="#c9cff0", lw=1.8)

agents = [
    ("Content", 70),
    ("Critical thinking", 62.5),
    ("Communication", 55),
    ("Leadership", 47.5),
    ("Collaboration", 40),
    ("Behavior", 32.5),
]
ax.text(
    62,
    75,
    "EVALUATOR AGENTS (LLM, parallel fan-out)\none call per dimension, all candidates",
    color="#9b8cff",
    fontsize=11,
    fontweight="bold",
    ha="center",
    va="top",
)
ev_boxes = []
for name, y in agents:
    ev_boxes.append(box(44, y - 9.5 if False else y - 8, 36, 6, name, "llm", size=10))
for b in ev_boxes:
    arrow(right(plan), left(b), color="#8f87d6", lw=1.1, rad=0.0)

# Evidence tools
tb = box(92, 30, 44, 44, "", "tool")
ax.text(114, 72, "EVIDENCE TOOLS (read-only)", color="#ff7aa8", fontsize=11, fontweight="bold", ha="center")
for i, line in enumerate(
    [
        "get_transcript_range(from, to)",
        "get_candidate_metrics(candidate)",
        "get_turns(candidate)",
        "get_interactions(candidate)",
        "find_quotes(keyword)",
    ]
):
    ax.text(94, 67 - i * 5.6, line, color=INK, fontsize=10, family="monospace", va="center")
ax.text(114, 34, "answers come from stored data and\ncode-computed metrics, never invented", color=MUTED, fontsize=9.5, ha="center", va="center")
for b in ev_boxes:
    arrow(right(b), (92, b[1] + 3), color="#ff7aa8", ls=":", lw=1.2, style="<|-|>")

coll = box(147, 52, 30, 14, "COLLECT\nresolve missing agents\n+ conflicts", "code", bold=True)
crit = box(147, 30, 30, 16, "CRITIC (audit)\nevidence timestamp must\nmatch the candidate\nspeaking", "code", bold=True)
rev = box(147, 8, 30, 14, "REVISE (max 1x)\nflagged evaluator re-runs\nwith the critic's reasons", "llm", bold=True)
arrow((136.5, 59), (146.5, 59), "6 verdicts\n(JSON, validated)", off=(0, 1.2))
arrow(bottom(coll), top(crit))
arrow(bottom(crit), top(rev), "evidence not\nverifiable", color="#ff6b6b", off=(10, -1))
arrow(left(rev), (80, 14), color="#ff6b6b", ls="--", rad=-0.0, lw=1.5)
arrow((80, 14), (62, 25), color="#ff6b6b", ls="--", lw=1.5)
ax.text(104, 12, "re-ask evaluator", color="#ff6b6b", fontsize=9.5, ha="center", va="bottom")

agg = box(190, 40, 38, 20, "AGGREGATE (no model)\nfixed weights:\nContent 30  Comm 20\nLeadership 20  Collab 20  Behavior 10\n− interruption penalty", "code", size=9.8, bold=True)
arrow(right(crit), (190, 47), "verified", off=(-1, 2.5))
arrow(right(coll), (190, 56), lw=1.5)
arrow(right(rev), (190, 44), color="#ff6b6b", ls="--", lw=1.4, rad=0.25)

pen = box(190, 14, 38, 17, "Interruption penalty\n−2 pts each, capped at −15\nfrom the turns table;\nagents cannot change it", "code", size=9.8)
arrow(top(pen), bottom(agg), color="#ff6b6b", lw=1.6)

syn = box(237, 52, 36, 18, "SYNTHESIS AGENT (LLM)\nnarrative per candidate\nreads the score through tools", "llm", size=10, bold=True)
stool = box(237, 30, 36, 15, "tools: compute_final_score\nscore_if(dimension, value)\nno numeric input accepted", "tool", size=9.5)
rep = box(237, 8, 36, 16, "REPORTS\ncandidate: score, evidence,\npenalty, metrics, narrative\ngroup report (host)", "store", size=9.8, bold=True)
arrow(right(agg), left(syn), "final\nscores", off=(0, 1.5))
arrow(bottom(syn), top(stool), style="<|-|>", color="#ff7aa8", ls=":")
arrow(bottom(stool), top(rep))

# ----------------------------------------------------------------- legend
lx, ly = 8, 0.2
for i, (label, kind) in enumerate(
    [
        ("LLM agent", "llm"),
        ("Deterministic code", "code"),
        ("Data store", "store"),
        ("External service", "ext"),
        ("Agent tool", "tool"),
    ]
):
    fill, edge = KINDS[kind]
    x = 8 + i * 48
    ax.add_patch(FancyBboxPatch((x, 0.2), 5, 2.4, boxstyle="round,pad=0.2", fc=fill, ec=edge, lw=1.6))
    ax.text(x + 7, 1.4, label, color=INK, fontsize=11, va="center")

fig.savefig(OUT, facecolor=BG, bbox_inches="tight", pad_inches=0.3)
print("wrote", OUT)
