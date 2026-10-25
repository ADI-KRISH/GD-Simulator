"""Draws the three system-design pictures in docs/: system_hld.png, agentic_design.png,
solo_practice_loop.png.

Run with a Python that has matplotlib:  python docs/make_system_diagrams.py

Purpose:    the high-level architecture, the agentic evaluation design and the solo
            practice loop, generated from code so they can be regenerated when the
            design changes. The words that go with them are in docs/SYSTEM_DESIGN.md.
Contains:   KINDS (colour per box kind), `canvas`, `box`, `arrow`, `lane`, `legend`, and
            one `draw_*` function per picture.
Depends on: matplotlib only.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

DOCS = Path(__file__).parent
BG, INK, MUTED = "#0b0e17", "#eef0f8", "#9aa3bd"
KINDS = {  # fill, edge
    "llm": ("#2a2159", "#9b8cff"),  # an LLM agent
    "code": ("#12304d", "#4aa3ff"),  # plain deterministic code
    "store": ("#103a30", "#3ecf9d"),  # something that stores data
    "ext": ("#43300f", "#ffb347"),  # outside this system
    "ui": ("#262b3d", "#9aa3bd"),  # what the user touches
}


def canvas(w: float, h: float, title: str):
    """A dark figure whose axes are in plain 0..w, 0..h coordinates."""
    fig, ax = plt.subplots(figsize=(w / 8, h / 8), dpi=130)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.axis("off")
    ax.text(2, h - 3, title, color=INK, fontsize=15, fontweight="bold", va="top")
    return fig, ax


def box(ax, x, y, w, h, text, kind="code", size=8.2):
    """A rounded, labelled box. Returns (x, y, w, h) for connecting arrows."""
    fill, edge = KINDS[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.25,rounding_size=1.2",
                                fc=fill, ec=edge, lw=1.4))  # fmt: skip
    ax.text(x + w / 2, y + h / 2, text, color=INK, fontsize=size, ha="center", va="center",
            linespacing=1.35)  # fmt: skip
    return (x, y, w, h)


def lane(ax, x, y, w, h, label, align="left"):
    """A dashed group outline with a caption in its top-left corner."""
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.3,rounding_size=1.5",
                                fc="none", ec="#3a4160", lw=1.1, ls=(0, (4, 3))))  # fmt: skip
    at, ha = (x + w - 1.2, "right") if align == "right" else (x + 1.2, "left")
    ax.text(at, y + h - 0.9, label, color=MUTED, fontsize=8.5, va="top", ha=ha, fontweight="bold")


def arrow(ax, a, b, label="", curve=0.0, color="#7e88a8", size=7.2, label_at=0.5, dy=0.9):
    """An arrow from point a to point b with an optional label near its middle."""
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=11, lw=1.2, color=color,
                                 connectionstyle=f"arc3,rad={curve}"))  # fmt: skip
    if label:
        x = a[0] + (b[0] - a[0]) * label_at
        y = a[1] + (b[1] - a[1]) * label_at + dy
        ax.text(x, y, label, color=MUTED, fontsize=size, ha="center", va="bottom",
                bbox=dict(fc=BG, ec="none", pad=1.2))  # fmt: skip


def right(b): return (b[0] + b[2], b[1] + b[3] / 2)  # noqa: E704
def left(b): return (b[0], b[1] + b[3] / 2)  # noqa: E704
def top(b): return (b[0] + b[2] / 2, b[1] + b[3])  # noqa: E704
def bottom(b): return (b[0] + b[2] / 2, b[1])  # noqa: E704


def legend(ax, x, y):
    """The colour key."""
    items = [("LLM agent", "llm"), ("Plain code", "code"), ("Stored data", "store"),
             ("Outside service", "ext"), ("User-facing", "ui")]  # fmt: skip
    for i, (name, kind) in enumerate(items):
        fill, edge = KINDS[kind]
        ax.add_patch(FancyBboxPatch((x + i * 24, y), 3.2, 2.2, boxstyle="round,pad=0.1",
                                    fc=fill, ec=edge, lw=1.2))  # fmt: skip
        ax.text(x + i * 24 + 4.6, y + 1.1, name, color=MUTED, fontsize=8, va="center")


def draw_hld() -> None:
    """Picture 1: the whole system in layers."""
    fig, ax = canvas(170, 113, "GD Platform: high-level architecture")

    lane(ax, 2, 90, 166, 14, "CLIENTS")
    ui = box(ax, 6, 92, 50, 8, "Next.js web app  :5214\nlobby · practice room · reports · dashboard", "ui")
    lk = box(ax, 62, 92, 44, 8, "LiveKit client\ngroup audio / video", "ui")
    pf = box(ax, 112, 92, 50, 8, "Platform shell (optional)\nembeds GD, signs students in", "ui")

    lane(ax, 2, 48, 166, 38, "GD API  ·  FastAPI, one process  :8114   (/api/v1/...)")
    cells = [  # (column, row, text, kind); row 0 is the upper row
        (0, 0, "auth\nregister · login · roles", "code"),
        (1, 0, "session + lobby\nstate machines · timer", "code"),
        (2, 0, "realtime hub\nWebSocket control plane", "code"),
        (3, 0, "practice (solo)\nroom loop · personas", "code"),
        (0, 1, "speech\nSTT · TTS · chunking", "code"),
        (1, 1, "metrics\ntalk time · interruptions\ninteraction graph", "code"),
        (2, 1, "observer\nrolling-state agent", "llm"),
        (3, 1, "evaluation\nLangGraph pipeline", "llm"),
    ]
    for col, row, text, kind in cells:
        box(ax, 5 + col * 39, 72 - row * 12, 36, 10, text, kind, 7.6)
    box(ax, 5, 51, 77, 6.5, "background loops: session ticker · observer loop · evaluation worker", "code", 7.4)
    box(ax, 86, 51, 74, 6.5, "observability (traces · alerts · datasets)  ·  notifications  ·  reports", "code", 7.4)

    lane(ax, 2, 4, 80, 38, "DATA")
    box(ax, 5, 24, 36, 12, "Database\nPostgres (or SQLite)\nusers · sessions · transcript\nscores · reports · traces", "store", 7.2)
    box(ax, 44, 24, 35, 12, "Redis\nevents · job queue\n(off in platform mode)", "store", 7.4)
    box(ax, 5, 7, 74, 12, "Object store: local files or MinIO\nraw audio chunks, deleted once the evaluation is done", "store", 7.4)

    lane(ax, 86, 4, 82, 38, "OUTSIDE SERVICES")
    box(ax, 89, 24, 37, 12, "LLM provider\nAnthropic / OpenAI-compatible\nevaluators · observer · personas", "ext", 7.2)
    box(ax, 129, 24, 36, 12, "Speech-to-text\nWhisper", "ext", 7.4)
    box(ax, 89, 7, 37, 12, "Text-to-speech\nEdge / ElevenLabs", "ext", 7.4)
    box(ax, 129, 7, 36, 12, "LiveKit server (group rooms)\nPlatform skill graph (M04)", "ext", 7.2)

    arrow(ax, (31, 92), (31, 86), "REST + WebSocket", dy=-0.3)
    arrow(ax, (84, 92), (84, 86), "media token", dy=-0.3)
    arrow(ax, (137, 92), (137, 86), "sign-in", dy=-0.3)
    arrow(ax, (42, 48), (42, 42), "SQL · queue · audio files", dy=-0.3)
    arrow(ax, (127, 48), (127, 42), "model · speech · skill-graph calls", dy=-0.3)
    legend(ax, 4, 0.2)
    fig.savefig(DOCS / "system_hld.png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)


def draw_agentic() -> None:
    """Picture 2: who thinks (LLM) and who calculates (code) in an evaluation."""
    fig, ax = canvas(190, 118, "Agentic evaluation design: LLMs judge, code decides the number")

    lane(ax, 2, 82, 186, 30, "1  DURING THE SESSION  (evidence is collected, nothing is scored)")
    stt = box(ax, 6, 90, 30, 14, "Audio chunks\n→ speech-to-text\n→ transcript", "code")
    obs = box(ax, 44, 90, 34, 14, "Observer agent\nevery ~45 s window\nrolling state: topics,\nclaims, who spoke", "llm")
    met = box(ax, 86, 90, 36, 14, "Metrics engine\ntalk time · turns · interruptions\nresponses · interaction graph", "code")
    per = box(ax, 130, 90, 54, 14, "Persona agents (solo mode)\nspeak as the panel; see the\nclock, never score anyone", "llm")
    arrow(ax, right(stt), left(obs))
    arrow(ax, right(obs), left(met))
    arrow(ax, right(met), left(per), color="#4a5170")

    lane(ax, 2, 20, 186, 58, "2  AFTER THE SESSION  ·  LangGraph, one job per session", align="right")
    plan = box(ax, 6, 50, 24, 16, "PLAN (plain code)\nlookup: 4-5 specialists\nper GD type; shared\ncontext + packet each", "code")
    spec = box(ax, 36, 40, 52, 30,
               "SPECIALIST AGENTS (4-5 of these 6, parallel)\n\n content · communication\n leadership · collaboration\n critical thinking · behaviour\n\n"
               "each returns: score, confidence,\nstrengths, weaknesses, evidence", "llm", 7.8)
    tools = box(ax, 36, 22, 52, 13,
                "EVIDENCE TOOLS (read-only code)\nget_transcript_range · get_turns · find_quotes\nget_candidate_metrics · get_interactions · verify_segment", "code", 7.4)
    col = box(ax, 96, 54, 22, 14, "COLLECT\nmissing results\nconflicts", "code")
    cri = box(ax, 96, 36, 22, 14, "CRITIC (code, no tools)\ntime real? quote real?\nspeaker certain?\nconfidence earned?", "code", 7.2)
    rev = box(ax, 96, 22, 22, 10, "REVISE once, parallel\nre-asks specialists\nwith critic hints", "llm", 6.8)
    agg = box(ax, 126, 46, 26, 22, "AGGREGATE\nfixed weights\ninterruption penalty\noverall 0-100\nreview flags", "code")
    syn = box(ax, 160, 54, 26, 14, "SYNTHESIS\nagent per candidate\nwrites the narrative", "llm")
    grp = box(ax, 160, 36, 26, 14, "GROUP REPORT\nagent: group story", "llm")
    rep = box(ax, 126, 22, 60, 10, "REPORTS: candidate + group · notifications · audio deleted", "store", 7.8)

    arrow(ax, (18, 82), top(plan), "transcript + metrics + rolling state", label_at=0.5, dy=0.2)
    arrow(ax, right(plan), left(spec))
    arrow(ax, bottom(spec), top(tools), "look up", dy=-0.4)
    arrow(ax, right(spec), left(col))
    arrow(ax, bottom(col), top(cri))
    arrow(ax, bottom(cri), top(rev), "problems + real lines", dy=-0.2)
    arrow(ax, left(rev), (66, 40), "re-ask, same tools", curve=-0.3, label_at=0.25, dy=-0.4)
    arrow(ax, right(cri), left(agg), curve=-0.15)
    arrow(ax, right(agg), left(syn))
    arrow(ax, bottom(syn), top(grp))
    arrow(ax, bottom(grp), (173, 32))
    arrow(ax, bottom(agg), (139, 32))

    lane(ax, 2, 2, 186, 14, "3  AFTER THE REPORT  ·  memory, oversight, measurement")
    box(ax, 6, 4, 42, 8, "Progress memory\nthis score vs your last 3 sessions\n(coaching only, never fed to judges)", "code", 7.2)
    box(ax, 54, 4, 40, 8, "Human review flags\nlow confidence · critic left issues\nagents disagree > 2 points", "code", 7.2)
    box(ax, 100, 4, 40, 8, "Skill graph (M04)\nscores + the student's own words\nsent only with consent", "ext", 7.2)
    box(ax, 146, 4, 40, 8, "Tracing & experiments\nevery call: prompt · tokens · cost · latency\ndatasets, replays, alerts", "store", 7.0)
    legend(ax, 4, 17.5)
    fig.savefig(DOCS / "agentic_design.png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)


def draw_solo() -> None:
    """Picture 3: one turn of solo practice, from the student's voice to a persona's reply."""
    fig, ax = canvas(190, 96, "Solo practice: one turn, end to end")

    lane(ax, 2, 62, 186, 28, "BROWSER")
    mic = box(ax, 6, 68, 30, 14, "Student speaks\nvoice detection (VAD)\nstart / stop signals", "ui")
    ws = box(ax, 44, 68, 30, 14, "WebSocket\nutterance audio\ntyped text · raise hand", "ui")
    play = box(ax, 118, 68, 34, 14, "Plays persona audio\nstops at once if the\nstudent talks (barge-in)", "ui")
    box(ax, 158, 68, 26, 14, "Captions\nlive transcript, timer\n(hub broadcasts)", "ui", 7.4)

    lane(ax, 2, 4, 186, 54, "SERVER  ·  PracticeRoom, one per student")
    stt = box(ax, 6, 36, 28, 14, "Speech-to-text\nWhisper\n(hold replies meanwhile)", "ext", 7.4)
    rec = box(ax, 40, 36, 30, 14, "Record the turn\nspeech event · turn\ntranscript segment", "store", 7.6)
    pan = box(ax, 76, 30, 42, 20, "PANEL LOGIC (code)\nwho speaks: owed turn →\nnamed persona → highest bid\nmoderator: open · invite quiet\nstudent · redirect · curveball\nclock: time checks, close", "code", 7.4)
    clk = box(ax, 76, 8, 42, 16, "CLOCK AWARENESS (code)\nphase: opening · middle ·\nwrap-up · final · over\nword budget fits the time left\nonly one persona concludes", "code", 7.4)
    llm = box(ax, 124, 36, 30, 14, "Persona agent (LLM)\nreturns speak? · text ·\nis_conclusion", "llm", 7.6)
    chk = box(ax, 124, 10, 30, 18, "Checks (code)\nrepeats a point? retry\nonce, then yield or\ncurveball · honest if\nasked 'are you an AI?'", "code", 7.0)
    tts = box(ax, 160, 36, 26, 14, "Text-to-speech\nstreamed in chunks\nfirst byte timed", "ext", 7.4)
    idl = box(ax, 6, 8, 36, 16, "Idle loop (every 1 s)\nopens the discussion,\nnudges a quiet room;\nless patience near the end", "code", 7.2)

    arrow(ax, right(mic), left(ws), "audio", dy=0.5)
    arrow(ax, (52, 68), top(stt), "audio", curve=0.15, label_at=0.45, dy=0.3)
    arrow(ax, (66, 68), top(rec), "typed text", label_at=0.5, dy=0.1)
    arrow(ax, left(play), right(ws), "barge-in: stop audio,\nrecord an interruption", dy=0.4, size=7)
    arrow(ax, right(stt), left(rec))
    arrow(ax, right(rec), (76, 44))
    arrow(ax, top(pan), top(llm), "prompt: persona, stance,\nclock, last 10 lines", curve=-0.25, dy=3.0)
    arrow(ax, bottom(llm), top(chk))
    arrow(ax, right(chk), (173, 36), "approved line", curve=0.0, label_at=0.55, dy=0.2)
    arrow(ax, top(tts), (135, 68), "audio chunks", curve=0.2, dy=0.5)
    arrow(ax, top(clk), bottom(pan), "")
    arrow(ax, right(idl), (76, 34), "starts a turn", curve=0.2, label_at=0.4, dy=0.3)
    legend(ax, 4, 0.0)
    fig.savefig(DOCS / "solo_practice_loop.png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    draw_hld()
    draw_agentic()
    draw_solo()
    print("wrote system_hld.png, agentic_design.png, solo_practice_loop.png")
