"""GD's results reach the platform's skill graph, M04, as M13's do (wrap-up Day 4).

For each candidate who came from the platform, once their report exists:

1. Their practice consent is read from M04 — M04 requires it on every
   observation, and without it nothing is sent.
2. Their own lines in the discussion are registered as an M04 artifact, so the
   evidence resolves (M04 refuses an observation whose evidence does not).
3. One `gd_contribution` observation per scored dimension, on the
   communication concept that dimension evidences, citing that artifact.

M04 records these — dated, counted, quoted in the skill graph's drawer — and by
its own rule (EC-4.17) does not move the ability estimate for evidence that has
no calibrated item behind it. That is the honest reading of a rubric score from
a practice discussion, and it is M04's call, not GD's.

Best-effort and off the student's path: M04 being down narrows nothing in GD.
The payload shapes copy M13's producer (`emit.py`) and M04's `ObservationIn`.
"""

import logging
import uuid
from typing import Any

import httpx
from ai_core import service as platform_service

from services.common.config import settings
from services.speech.stt import get_stt_provider

logger = logging.getLogger(__name__)

#: What each GD dimension is evidence of, in M04's ontology (its communication
#: domain), matched on the dimension's own rubric. A test reads M04's ontology
#: beside this repo and checks each exists. One concept per dimension: M04 keys
#: an observation on (session, concept, kind), so two would collide.
#:
#: Two dimensions have no honest match and are not sent. `content` is the
#: substance of a topic M04 has no concept for; `leadership` (initiating,
#: moving the group on) is not "Being Concise", the nearest name. They stay in
#: GD's own report.
DIMENSION_CONCEPTS: dict[str, str] = {
    "communication": "concept:answer_structuring",  # clarity, structure
    "critical_thinking": "concept:explaining_tradeoffs",  # weighing trade-offs
    "collaboration": "concept:active_listening",  # listening, building on ideas
    "behavior": "concept:handling_interruptions",  # interruptions, turn-taking
}

ARTIFACT_TYPE = "gd_transcript"
TIMEOUT_SECONDS = 10.0


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def own_lines(segments: list[Any], label: str) -> str:
    """The candidate's own words, in order — the text M04 quotes evidence from."""
    return "\n".join(
        segment.text.strip()
        for segment in sorted(segments, key=lambda s: s.start)
        if segment.candidate_id == label and (segment.text or "").strip()
    )


def observations_for(
    subject: str,
    session_id: str,
    dimensions: dict[str, float],
    artifact: dict[str, str],
    consent_id: str,
) -> list[dict[str, Any]]:
    """One observation per dimension GD scored and M04 has a concept for."""
    evidence = [
        {
            "kind": "artifact",
            "session_id": session_id,
            "artifact_id": artifact["artifact_id"],
            "checksum": artifact["checksum"],
        }
    ]
    return [
        {
            "candidate_id": subject,
            "concept_id": DIMENSION_CONCEPTS[dimension],
            "session_id": session_id,
            "evidence": evidence,
            "kind": "gd_contribution",
            "response_model": "none",
            "outcome": round(max(0.0, min(score, 10.0)) / 10.0, 4),
            "source_dimension_key": dimension,
            "consent_id": consent_id,
        }
        for dimension, score in sorted(dimensions.items())
        if dimension in DIMENSION_CONCEPTS
    ]


async def send_candidate(
    subject: str,
    session_id: str,
    dimensions: dict[str, float],
    lines: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Send one candidate's results to M04. Returns how many observations it took."""
    if not lines or not any(d in DIMENSION_CONCEPTS for d in dimensions):
        return 0  # nothing they said to quote, or nothing scored that M04 tracks
    if not _is_uuid(subject) or not _is_uuid(session_id):
        # M04 keys students and sessions by UUID and would refuse the batch.
        logger.info("not a platform UUID; GD results not sent", extra={"subject": subject})
        return 0
    headers = {
        **platform_service.headers(),
        "X-Candidate-Id": subject,
        "X-Role": "student",
    }
    base = f"{settings.m04_url.rstrip('/')}/v1/candidates/{subject}"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        consents = await client.get(
            f"{base}/consent", params={"purpose": "practice"}, headers=headers
        )
        consents.raise_for_status()
        active = [c for c in consents.json() if c.get("active")]
        if not active:
            logger.info(
                "no practice consent in M04; GD results not sent", extra={"subject": subject}
            )
            return 0
        artifact = await client.post(
            f"{base}/artifacts",
            json={
                "candidate_id": subject,
                "session_id": session_id,
                "artifact_type": ARTIFACT_TYPE,
                "content": lines,
            },
            headers=headers,
        )
        artifact.raise_for_status()
        batch = observations_for(
            subject, session_id, dimensions, artifact.json(), active[0]["consent_id"]
        )
        recorded = await client.post(
            f"{base}/observations", json={"observations": batch}, headers=headers
        )
        recorded.raise_for_status()
        return len(batch)
    finally:
        if owns_client:
            await client.aclose()


async def send_results(
    session_id: str,
    scores: dict[str, Any],
    segments: list[Any],
    subjects: dict[str, str],
) -> int:
    """Every platform candidate's results; never raises. `subjects`: label -> subject."""
    if not settings.platform_bridge or not subjects:
        return 0
    if get_stt_provider().name == "stub":
        # The stub hears every spoken turn as one of six canned sentences. Scored,
        # those are not the student's words, and quoted to M04 they would be
        # evidence of nothing. Typed turns are real, but nothing records which
        # turns were typed, so the whole session stays in GD.
        logger.warning(
            "speech-to-text is the stub; GD results not sent to the skill graph",
            extra={"session_id": session_id},
        )
        return 0
    sent = 0
    for label, subject in subjects.items():
        score = scores.get(label)
        if score is None:
            continue
        try:
            sent += await send_candidate(
                subject, session_id, dict(score.dimensions), own_lines(segments, label)
            )
        except Exception as exc:  # M04 down or refusing: GD's report stands regardless
            logger.warning(
                "GD results did not reach the skill graph",
                extra={"subject": subject, "error": str(exc)},
            )
    if sent:
        logger.info("GD results sent to the skill graph", extra={"observations": sent})
    return sent
