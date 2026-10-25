"""The span table: one row per model call, speech call or measured latency.

Purpose:     Stores what the tracer saw, including the full prompt and output.
Why needed:  Debugging an agent means reading exactly what it was asked and what it
             said, next to what that cost and how long it took.
Note:        No foreign keys: spans are a log and should outlive a deleted session.
             `trace_store_content=false` keeps timings and tokens but drops the text.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from services.common.db import Base


class Span(Base):
    """One recorded call."""

    __tablename__ = "trace_spans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # llm | stt | tts | latency
    kind: Mapped[str] = mapped_column(String(12), index=True)
    # evaluation | synthesis | persona | observer | ... (the caller's reason)
    purpose: Mapped[str] = mapped_column(String(24), index=True, default="unknown")
    name: Mapped[str] = mapped_column(String(60), default="")
    provider: Mapped[str] = mapped_column(String(20), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    run: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    dimension: Mapped[str | None] = mapped_column(String(24), nullable=True)
    candidate: Mapped[str | None] = mapped_column(String(8), nullable=True)

    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    # Time to first byte, where it exists (text-to-speech).
    ttfb_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    priced: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(8), index=True, default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
