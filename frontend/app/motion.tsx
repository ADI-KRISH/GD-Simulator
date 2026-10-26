"use client";

import { useAfterMount, useCountUp } from "@/lib/use-motion";

/** A progress bar that grows to its value when it appears. */
export function AnimatedBar({
  value,
  tone,
  className = "",
}: {
  /** 0 to 100. */
  value: number;
  tone: "good" | "mid" | "low";
  className?: string;
}) {
  const width = useAfterMount(Math.max(0, Math.min(100, value)));
  return (
    <div className={`bar bar--${tone} ${className}`}>
      <span style={{ width: `${width}%` }} />
    </div>
  );
}

/** A whole number that counts up to its value. */
export function CountUp({ value, suffix = "" }: { value: number; suffix?: string }) {
  const shown = useCountUp(value);
  return (
    <>
      {Math.round(shown)}
      {suffix}
    </>
  );
}

/** A circular score that sweeps round and counts up together. */
export function ScoreRing({
  score,
  toneClass,
}: {
  score: number;
  toneClass: string;
}) {
  const p = useAfterMount(Math.max(0, Math.min(100, score)));
  return (
    <div
      className={`score-ring score-ring--animated ${toneClass}`}
      style={{ "--p": p } as React.CSSProperties}
      role="img"
      aria-label={`Overall ${Math.round(score)} out of 100`}
    >
      <span>
        <strong>
          <CountUp value={score} />
        </strong>
        <small>/ 100</small>
      </span>
    </div>
  );
}

/** Placeholder blocks shown while a page loads. */
export function Skeleton({ rows = 3, tall = false }: { rows?: number; tall?: boolean }) {
  return (
    <div className="stack" style={{ gap: "1rem" }} aria-busy="true" aria-label="Loading">
      <div className="skeleton skeleton--title" />
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className={`skeleton ${tall ? "skeleton--tall" : ""}`} />
      ))}
    </div>
  );
}
