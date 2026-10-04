"""Simulated GD: three LLM-played candidates, evaluated end to end.

Tests the evaluation system without microphones. Three agents with deliberately
different conduct hold a discussion; the transcript is injected through the
local-only `/transcript/seed` endpoint (the same rows the speech pipeline
writes), the session is ended, and the real evaluation worker scores it.

The personas are built so the right answer is known in advance:

    A  Dominator     — talks most, interrupts, never builds on anyone
    B  Facilitator   — builds on others by name, summarises, draws people in
    C  Quiet expert  — speaks rarely, briefly, with specific evidence

The run then checks that the scores agree with how each persona behaved.

Requires the backend running on localhost:8000 (APP_ENV=local) and an OpenAI
key in .env. Run from the repo root:

    ./.venv/Scripts/python.exe -m scripts.simulate_gd --turns 18
"""

import argparse
import random
import time
import uuid

import httpx
from openai import OpenAI

from services.common.config import settings

API = "http://127.0.0.1:8000/api/v1"
PASSWORD = "simulated-candidate-pw"

PERSONAS = {
    "A": (
        "Arjun",
        0.50,  # share of turns
        "You are Arjun, a candidate who dominates group discussions. Speak in long, "
        "confident monologues (4-6 sentences). Repeat your own position, make sweeping "
        "claims without evidence, and never acknowledge or build on what others said. "
        "Sometimes cut in mid-thought with 'No, no, listen—'.",
    ),
    "B": (
        "Bhavana",
        0.33,
        "You are Bhavana, a candidate who facilitates. Speak in 2-3 sentences. Refer to "
        "others by name and build on their points ('Building on what Chitra said...'), "
        "summarise where the group stands, weigh trade-offs, and invite quieter people "
        "in by asking them direct questions.",
    ),
    "C": (
        "Chitra",
        0.17,
        "You are Chitra, a quiet but knowledgeable candidate. Speak in 1-2 short "
        "sentences only. Each contribution adds one specific, accurate fact, number or "
        "example that nobody else mentioned. Never ramble.",
    ),
}

WORDS_PER_SECOND = 2.6


def say(llm: OpenAI, label: str, topic: str, lines: list[tuple[str, str]]) -> str:
    name, _, persona = PERSONAS[label]
    names = {k: v[0] for k, v in PERSONAS.items()}
    history = "\n".join(f"{names[who]}: {text}" for who, text in lines) or "(nobody has spoken)"
    response = llm.chat.completions.create(
        model=settings.llm_model,
        temperature=0.9,
        messages=[
            {
                "role": "system",
                "content": f"{persona}\nGroup discussion topic: {topic}\n"
                f"Reply with only what {name} says next — no name prefix, no stage directions.",
            },
            {"role": "user", "content": f"Discussion so far:\n{history}\n\n{name}:"},
        ],
    )
    return response.choices[0].message.content.strip().strip('"')


def pick_speaker(history: list[str]) -> str:
    # Rare, not absent: a silent candidate would score zero everywhere and test
    # nothing, so everyone gets the floor at least once every five turns.
    for label in PERSONAS:
        if len(history) >= 4 and label not in history[-4:]:
            return label
    labels = [
        k for k in PERSONAS if not history or k != history[-1] or k == "A"
    ]  # only A hogs consecutive turns
    return random.choices(labels, weights=[PERSONAS[k][1] for k in labels])[0]


def script_discussion(topic: str, turns: int) -> list[dict]:
    llm = OpenAI(api_key=settings.llm_api_key or settings.openai_api_key or None)
    lines: list[tuple[str, str]] = []
    segments: list[dict] = []
    clock, speakers = 2.0, []
    for turn in range(turns):
        label = pick_speaker(speakers) if turn else "A"
        text = say(llm, label, topic, lines)
        duration = max(2.0, len(text.split()) / WORDS_PER_SECOND)
        start = clock + random.uniform(0.4, 1.5)
        # The dominator talks over the end of the previous speaker. Overlapping
        # speech is what the metrics engine counts as an interruption.
        if (
            label == "A"
            and segments
            and segments[-1]["candidate_label"] != "A"
            and random.random() < 0.6
        ):
            start = max(segments[-1]["start"] + 1.0, segments[-1]["end"] - 2.0)
        end = start + duration
        segments.append(
            {"candidate_label": label, "start": round(start, 2), "end": round(end, 2), "text": text}
        )
        lines.append((label, text))
        clock = max(clock, end)
        speakers.append(label)
        print(
            f"  [{start:6.1f}s] {label} {PERSONAS[label][0]}: "
            f"{text[:110]}{'…' if len(text) > 110 else ''}"
        )
    return segments


def account(client: httpx.Client, tag: str, role: str) -> dict:
    email = f"sim-{tag}-{uuid.uuid4().hex[:8]}@example.com"
    client.post(
        f"{API}/auth/register",
        json={"email": email, "full_name": tag, "password": PASSWORD, "role": role},
    ).raise_for_status()
    token = (
        client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
        .raise_for_status()
        .json()["access_token"]
    )
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--turns", type=int, default=18)
    parser.add_argument(
        "--gd-type", default="general", choices=["general", "technical", "leadership", "hr_social"]
    )
    parser.add_argument("--topic", default="Should AI tools replace junior software developers?")
    parser.add_argument("--seed", type=int, default=None, help="random seed for turn order")
    args = parser.parse_args()
    random.seed(args.seed)

    with httpx.Client(timeout=60) as client:
        try:
            client.get("http://127.0.0.1:8000/health")
        except httpx.ConnectError:
            print("backend is not running on 127.0.0.1:8000 — start it first (see README §2)")
            return 2
        host = account(client, "host", "host")
        candidates = {label: account(client, PERSONAS[label][0], "candidate") for label in PERSONAS}

        gd = (
            client.post(
                f"{API}/sessions",
                headers=host,
                json={
                    "topic": args.topic,
                    "gd_type": args.gd_type,
                    "min_participants": 3,
                    "max_participants": 3,
                    "duration_seconds": 900,
                    "preparation_seconds": 0,
                    "video_required": False,
                },
            )
            .raise_for_status()
            .json()
        )
        sid = gd["id"]
        print(f"session {sid} ({args.gd_type}), code {gd['code']}")

        # Join in label order so persona A really is candidate A.
        for label in PERSONAS:
            joined = (
                client.post(
                    f"{API}/lobbies/join", headers=candidates[label], json={"code": gd["code"]}
                )
                .raise_for_status()
                .json()
            )
            assigned = next(
                p["candidate_label"]
                for p in joined["participants"]
                if p["display_name"] == PERSONAS[label][0]
            )
            assert assigned == label, f"{PERSONAS[label][0]} got label {assigned}, expected {label}"
            client.post(
                f"{API}/lobbies/{sid}/ready", headers=candidates[label], json={"ready": True}
            ).raise_for_status()
        client.post(f"{API}/sessions/{sid}/start", headers=host).raise_for_status()

        print(f"\nscripting {args.turns} turns with {settings.llm_model}…")
        segments = script_discussion(args.topic, args.turns)
        client.post(
            f"{API}/sessions/{sid}/transcript/seed", headers=host, json={"segments": segments}
        ).raise_for_status()
        client.post(f"{API}/sessions/{sid}/end", headers=host).raise_for_status()

        print("\nwaiting for the evaluation worker…")
        deadline, state = time.time() + 600, None
        while time.time() < deadline:
            status = client.get(f"{API}/sessions/{sid}/evaluation", headers=host)
            if status.is_success and status.json()["state"] != state:
                state = status.json()["state"]
                print(
                    f"  {state}  agents={status.json()['selected_agents']}  "
                    f"error={status.json()['error']}"
                )
                if state in ("COMPLETED", "FAILED"):
                    break
            time.sleep(3)
        if state != "COMPLETED":
            print(f"evaluation did not complete (last state: {state})")
            return 1

        metrics = (
            client.get(f"{API}/sessions/{sid}/metrics", headers=host)
            .raise_for_status()
            .json()["candidates"]
        )
        scores = {
            s["candidate_label"]: s
            for s in client.get(f"{API}/sessions/{sid}/scores", headers=host)
            .raise_for_status()
            .json()
        }

    dims = sorted({d for s in scores.values() for d in s["dimensions"]})
    print(f"\n{'':14}{'overall':>8}{'share':>7}{'intr':>5}" + "".join(f"{d[:8]:>10}" for d in dims))
    for label, (name, *_) in PERSONAS.items():
        s, m = scores[label], metrics[label]
        print(
            f"{label} {name:<12}{s['overall']:>8.1f}"
            f"{m['participation_ratio'] * 100:>6.0f}%{m['interruptions']:>5}"
            + "".join(f"{s['dimensions'].get(d, float('nan')):>10.1f}" for d in dims)
            + ("  [needs review]" if s["needs_human_review"] else "")
        )

    def dim(label: str, name: str) -> float:
        return scores[label]["dimensions"].get(name, float("nan"))

    # What a correct evaluator must conclude from these personas. The first two
    # are measured in code; the rest are the agents' judgement.
    checks = [
        (
            "A holds the largest speaking share",
            max(metrics, key=lambda k: metrics[k]["participation_ratio"]) == "A",
        ),
        ("A interrupts the most", max(metrics, key=lambda k: metrics[k]["interruptions"]) == "A"),
        ("B out-collaborates A", dim("B", "collaboration") > dim("A", "collaboration")),
        ("B's conduct beats A's", dim("B", "behavior") > dim("A", "behavior")),
        ("C's content is at least A's", dim("C", "content") >= dim("A", "content")),
    ]
    if "leadership" in dims:
        checks.append(
            (
                "speaking most is not leadership: B > A",
                dim("B", "leadership") > dim("A", "leadership"),
            )
        )

    print()
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print(f"\nreports: http://localhost:3000/report/{sid}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
