"use client";

import { useEffect, useRef } from "react";  // scroll ref and effect

import type { TranscriptOut } from "@/lib/types";  // transcript type

/** The raw transcript (FR-15). Shared by the live room and the report. */
export function TranscriptCard({
  transcript,
  live = false,
}: {
  transcript: TranscriptOut;
  /** Follow new lines as they arrive. */
  live?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (live && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [live, transcript.lines.length]);

  return (
    <div className="card">
      <div className="row row--between" style={{ marginBottom: "0.7rem" }}>
        <h2 style={{ fontSize: "1.05rem", margin: 0 }}>
          {live ? "Live transcript" : "Transcript"}
        </h2>
        <span className="meta">
          {transcript.segment_count} segments · {transcript.chunk_count} processing windows
        </span>
      </div>
      {transcript.lines.length === 0 ? (
        <p className="empty">
          {live
            ? "Lines appear here as each 45-second window is transcribed."
            : "No transcript was captured for this session."}
        </p>
      ) : (
        <div className="transcript" ref={scrollRef}>
          {transcript.lines.map((line, index) => (
            <div className="transcript-line" key={index}>
              <time>{line.timestamp}</time>
              <strong>{line.candidate_label}</strong>
              <span>{line.text}</span>
            </div>
          ))}
        </div>
      )}
      <p className="footnote">
        {live
          ? "Each window is transcribed after it is recorded, so lines arrive about 45 seconds behind."
          : "The raw transcript is the source of truth. Every evaluator read this, not a summary of it."}
      </p>
    </div>
  );
}
