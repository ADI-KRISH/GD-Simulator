"""Processing windows for the Observer loop (FR-16, design §14.2, §15).

The hybrid chunking architecture (design §14) has four layers. This module owns
exactly one of them — the *processing chunk*: a fixed wall-clock window over the
raw transcript, containing only speech that is newly available.

What it deliberately does not do is replace the raw transcript. Chunks are a
window onto it (claude.md invariant 3); dropping every chunk would lose nothing
that is not still on disk in `transcript_segments`.
"""

from dataclasses import dataclass

from services.common.contracts import TranscriptSegment

# Design §14.2: "every 30-60 seconds". 45s sits in the middle: long enough that
# an LLM call per chunk stays affordable, short enough that the rolling state
# never lags the discussion by much.
CHUNK_SECONDS = 45.0


@dataclass(frozen=True, slots=True)
class Chunk:
    """One processing window."""

    index: int
    start: float
    end: float
    segments: list[TranscriptSegment]

    @property
    def is_empty(self) -> bool:
        return not self.segments

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for segment in self.segments:
            if segment.candidate_id not in seen:
                seen.append(segment.candidate_id)
        return seen

    def as_dialogue(self) -> str:
        """Render the window the way the Observer prompt expects to read it.

        Timestamps are mm:ss because that is the format the evaluation_output
        contract requires evidence to cite, so an agent quoting a line can copy
        the timestamp straight through.
        """
        return "\n".join(
            f"{format_timestamp(s.start)} {s.candidate_id}: {s.text}"
            for s in sorted(self.segments, key=lambda s: s.start)
        )


def format_timestamp(seconds: float) -> str:
    """Seconds since the discussion started, as mm:ss (design §50 evidence)."""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_timestamp(value: str) -> float:
    """Inverse of `format_timestamp`, tolerant of hh:mm:ss."""
    parts = [int(p) for p in value.strip().split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds)


def chunk_index_for(seconds: float, chunk_seconds: float = CHUNK_SECONDS) -> int:
    """Which window a moment falls into. Windows are [n*w, (n+1)*w)."""
    return int(max(0.0, seconds) // chunk_seconds)


def build_chunk(
    segments: list[TranscriptSegment],
    index: int,
    chunk_seconds: float = CHUNK_SECONDS,
) -> Chunk:
    """Collect the segments belonging to one window.

    A segment is assigned by its *start* time, so an utterance that runs across
    a boundary stays whole in the window it began in rather than being split.
    """
    start = index * chunk_seconds
    end = start + chunk_seconds
    members = [s for s in segments if start <= s.start < end]
    return Chunk(index=index, start=start, end=end, segments=sorted(members, key=lambda s: s.start))


def build_chunks(
    segments: list[TranscriptSegment], chunk_seconds: float = CHUNK_SECONDS
) -> list[Chunk]:
    """Every non-empty window covering the given segments."""
    if not segments:
        return []
    last = chunk_index_for(max(s.start for s in segments), chunk_seconds)
    chunks = [build_chunk(segments, i, chunk_seconds) for i in range(last + 1)]
    return [c for c in chunks if not c.is_empty]


def closed_chunk_indices(elapsed_seconds: float, chunk_seconds: float = CHUNK_SECONDS) -> list[int]:
    """Windows that are fully in the past and safe to process.

    The current, still-filling window is excluded: handing the Observer a
    half-collected window would make its update depend on when the loop
    happened to run.
    """
    completed = int(max(0.0, elapsed_seconds) // chunk_seconds)
    return list(range(completed))
