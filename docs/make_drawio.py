"""Write draw.io versions of the three system diagrams: docs/*.drawio.

    python docs/make_drawio.py

Open a file in draw.io (File > Open from > Device). Same colour key as the PNGs.
"""

from pathlib import Path
from xml.sax.saxutils import quoteattr

OUT = Path(__file__).parent
KIND = {  # fill, stroke
    "llm": ("#E1D5F5", "#7E57C2"),
    "code": ("#DAE8FC", "#6C8EBF"),
    "data": ("#D5E8D4", "#82B366"),
    "ext": ("#FFE6CC", "#D79B00"),
    "user": ("#F5F5F5", "#666666"),
}
LEGEND = [("llm", "LLM agent"), ("code", "Deterministic code"), ("data", "Stored data"),
          ("ext", "Outside service"), ("user", "What the user touches")]  # fmt: skip


class Diagram:
    def __init__(self, name: str) -> None:
        self.name, self.cells = name, []

    def _add(self, label: str, style: str, x: int, y: int, w: int, h: int, **attrs: str) -> str:
        cid = f"c{len(self.cells) + 2}"
        extra = "".join(f" {k}={quoteattr(v)}" for k, v in attrs.items())
        self.cells.append(
            f'<mxCell id="{cid}" value={quoteattr(label.replace(chr(10), "<br>"))} '
            f'style="{style}" vertex="1" parent="1"{extra}>'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        return cid

    def box(self, label: str, x: int, y: int, w: int = 170, h: int = 60, kind: str = "code") -> str:
        fill, stroke = KIND[kind]
        style = f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};fontSize=12;"
        return self._add(label, style, x, y, w, h)

    def group(self, label: str, x: int, y: int, w: int, h: int) -> str:
        """Dashed frame with a title; draw it before the boxes it holds."""
        style = ("rounded=1;whiteSpace=wrap;html=1;fillColor=none;dashed=1;strokeColor=#999999;"
                 "verticalAlign=top;align=left;spacingLeft=10;fontStyle=1;fontSize=13;")  # fmt: skip
        return self._add(label, style, x, y, w, h)

    def edge(self, a: str, b: str, label: str = "", dashed: bool = False, both: bool = False) -> None:
        style = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;fontSize=11;"
        style += "dashed=1;" if dashed else ""
        style += "startArrow=block;" if both else ""
        self.cells.append(
            f'<mxCell id="e{len(self.cells) + 2}" value={quoteattr(label)} style="{style}" '
            f'edge="1" parent="1" source="{a}" target="{b}"><mxGeometry relative="1" as="geometry"/></mxCell>'
        )

    def legend(self, x: int, y: int) -> None:
        for i, (kind, text) in enumerate(LEGEND):
            self.box(text, x + i * 190, y, 170, 36, kind)

    def xml(self) -> str:
        return (
            f'<mxfile><diagram name={quoteattr(self.name)}>'
            '<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" page="1" pageWidth="1200" '
            'pageHeight="1000"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            + "".join(self.cells) + "</root></mxGraphModel></diagram></mxfile>"
        )  # fmt: skip

    def save(self, filename: str) -> None:
        (OUT / filename).write_text(self.xml(), encoding="utf-8")
        print("wrote", filename)


def hld() -> None:
    d = Diagram("High-level design")
    web = d.box("Next.js web app\nlobby · practice room · reports · observability", 30, 40, 240, 70, "user")
    lk = d.box("LiveKit client\ngroup audio and video", 290, 40, 220, 70, "user")
    shell = d.box("Platform shell (optional)\nembeds GD, students arrive signed in", 530, 40, 230, 70, "user")
    back = d.group("Backend: one FastAPI process (services.main:app, port 8114)", 30, 170, 730, 295)
    cells = [
        ("auth\naccounts · roles", "code"), ("session + lobby\nstate machine · timer", "code"),
        ("realtime\nWebSocket hub", "code"), ("practice (solo)\nroom loop · personas", "code"),
        ("speech\nSTT · TTS · chunking", "code"), ("metrics\ntalk time · interruptions", "code"),
        ("observer\nrolling-state agent", "llm"), ("evaluation\nLangGraph pipeline", "llm"),
    ]  # fmt: skip
    for i, (text, kind) in enumerate(cells):
        d.box(text, 45 + (i % 4) * 177, 210 + (i // 4) * 80, 165, 65, kind)
    d.box("observability (tracing, alerts, experiments) · notifications", 45, 370, 700, 35)
    d.box("Background loops: session ticker · observer loop · evaluation worker", 45, 415, 700, 35)
    db = d.box("Database\nPostgres (SQLite in platform mode)\ntranscripts, scores, reports, spans", 30, 520, 230, 80, "data")
    redis = d.box("Redis\nevent bus + job queue", 280, 520, 230, 80, "data")
    store = d.box("Object store (disk or MinIO)\naudio, deleted after evaluation", 530, 520, 230, 80, "data")
    ext = [
        "LLM provider\nevaluators · observer · personas", "Speech-to-text\nWhisper",
        "Text-to-speech\nEdge or ElevenLabs", "LiveKit server\ngroup rooms",
        "Platform skill graph (M04)\nonly with consent",
    ]  # fmt: skip
    for i, text in enumerate(ext):
        e = d.box(text, 830, 170 + i * 82, 250, 60, "ext")
        d.edge(back, e)
    for client in (web, lk, shell):
        d.edge(client, back)
    for store_id in (db, redis, store):
        d.edge(back, store_id)
    d.legend(30, 650)
    d.save("system_hld.drawio")


def agentic() -> None:
    d = Diagram("Agentic evaluation design")
    s1 = d.group("Stage 1 · during the session (evidence, no scoring)", 30, 30, 1040, 200)
    audio = d.box("Audio chunks", 50, 70, kind="user")
    stt = d.box("Speech-to-text", 250, 70, kind="ext")
    tr = d.box("Transcript segments\nspeaker-attributed", 450, 70, kind="data")
    me = d.box("Metrics engine\ntalk time · turns · interruptions", 700, 70)
    ob = d.box("Observer agent\nrolling summary", 450, 160, h=50, kind="llm")
    d.edge(audio, stt)
    d.edge(stt, tr)
    d.edge(tr, me, "speech events")
    d.edge(tr, ob)
    d.group("Stage 2 · after the session (LangGraph state graph)", 30, 260, 1040, 400)
    plan = d.box("Plan (plain code, no LLM)\nlookup: 4-5 specialists per GD type;\nshared context + packet per candidate", 50, 320, h=70)
    spec = d.box("Specialist agents (4-5 of 6), in parallel\ncontent · communication · leadership\ncollaboration · critical thinking · behaviour", 250, 305, 190, 100, "llm")  # fmt: skip
    tools = d.box("Evidence tools (read-only)\ntranscript range · turns · find quotes\nmetrics · interactions · verify segment", 250, 450, 190, 80)  # fmt: skip
    collect = d.box("Collect\nusable · failed · conflicts (>2 pts)", 490, 320)
    critic = d.box("Critic (code, no tools)\ntimes, quotes, speaker, confidence;\nhints: real speaker + real lines", 680, 320, 180)
    agg = d.box("Aggregate (code)\nfixed weights · penalty · review flags", 880, 320, 170)
    rev = d.box("Revise, once, in parallel\nre-asks flagged + failed specialists\nwith critic hints, same tools", 680, 450, 180, kind="llm")
    narr = d.box("Synthesis agent per candidate\n+ group report agent", 880, 450, 170, kind="llm")
    rep = d.box("Reports stored\nusers notified", 680, 570, 180, kind="data")
    wipe = d.box("Delete raw audio\ntranscripts and reports stay", 440, 570, 200)
    d.edge(s1, plan, "evidence")
    d.edge(plan, spec)
    d.edge(spec, tools, "tool calls", dashed=True, both=True)
    d.edge(spec, collect)
    d.edge(collect, critic)
    d.edge(critic, agg, "ok")
    d.edge(critic, rev, "flagged")
    d.edge(rev, collect, "revised")
    d.edge(agg, narr, "scores, flags")
    d.edge(narr, rep)
    d.edge(rep, wipe)
    s3 = d.group("Stage 3 · after the report (memory, oversight, measurement)", 30, 690, 1040, 140)
    names = [("Progress memory\nvs mean of last 3 sessions;\nnever shown to judges", "code"),
             ("Human review\nflagged candidates + reasons", "code"),
             ("Skill graph (M04)\nonly with consent", "ext"),
             ("Tracing + experiments\nsee observability", "code")]  # fmt: skip
    for i, (text, kind) in enumerate(names):
        d.box(text, 50 + i * 255, 730, 225, 75, kind)
    d.edge(rep, s3)
    d.legend(30, 860)
    d.save("agentic_design.drawio")


def solo() -> None:
    d = Diagram("Solo practice loop")
    idle = d.box("Idle loop (every second)\nopens discussion, nudges a quiet room", 630, 10, 200, 55)
    speak = d.box("Student speaks\nbrowser voice detection;\nor typed turn / raise hand", 30, 110, 190, 70, "user")
    stt = d.box("Server transcribes\nreplies held meanwhile", 250, 110, 190, 70, "ext")
    rec = d.box("Record the turn\nspeech event · turn · transcript segment", 470, 110, 190, 70, "data")
    panel = d.box("Panel logic\nowed turn → addressed by name\n→ highest bid", 690, 110, 190, 70)
    clock = d.box("Clock-aware prompt\nphase · time left · word budget\nwho has concluded", 910, 110, 190, 70)
    persona = d.box("Persona agent\nspeak? line · concludes?", 910, 270, 190, 70, "llm")
    check = d.box("Repeat check\nrepeat → sent back once;\nthen yield; 3 yields → new question", 690, 270, 190, 70)
    tts = d.box("Text-to-speech\nstreamed in chunks", 470, 270, 190, 70, "ext")
    play = d.box("Browser plays audio", 250, 270, 190, 70, "user")
    floor = d.box("Floor released\nwhen playback ends", 30, 270, 190, 70)
    barge = d.box("Barge-in\nstudent speaks over the persona;\naudio cut at once", 250, 430, 190, 70, "user")
    pen = d.box("Recorded as an interruption\nsame penalty as a group session", 470, 430, 190, 70, "data")
    d.edge(idle, panel)
    for a, b in [(speak, stt), (stt, rec), (rec, panel), (panel, clock), (clock, persona),
                 (persona, check), (check, tts), (tts, play), (play, floor)]:  # fmt: skip
        d.edge(a, b)
    d.edge(check, persona, "retry", dashed=True)
    d.edge(floor, speak, "next turn", dashed=True)
    d.edge(play, barge, dashed=True)
    d.edge(barge, pen)
    d.legend(30, 560)
    d.save("solo_practice_loop.drawio")


if __name__ == "__main__":
    hld()
    agentic()
    solo()
