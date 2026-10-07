"use client";

/** Building blocks of the Arena: orbs, the round table, the ring clock, notices. */

import type { CSSProperties, ReactNode } from "react";

import type { TurnNotice } from "@/lib/use-session-socket";

/** A stable hue per label, spread around the wheel and kept clear of amber. */
export function hueFor(label: string): number {
  let hash = 0;
  for (const ch of label) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  const hue = (hash * 47 + 200) % 360;
  return hue > 20 && hue < 60 ? hue + 70 : hue;
}

export interface SeatInfo {
  label: string;
  name: string;
  /** What to show under the name when not speaking. */
  sub?: string;
  speaking: boolean;
  /** 0..1 voice level; omit to let the CSS rings carry the animation. */
  level?: number;
  you?: boolean;
  badge?: string;
  /** Interruptions made, shown as a coral counter on the orb. */
  strikes?: number;
  away?: boolean;
  hue?: number;
}

export function Orb({ seat, children }: { seat: SeatInfo; children?: ReactNode }) {
  const style = {
    "--h": seat.hue ?? hueFor(seat.label),
    "--lvl": seat.speaking ? Math.max(seat.level ?? 0.35, 0.12) : (seat.level ?? 0) * 0.5,
  } as CSSProperties;
  return (
    <div className={`orb ${seat.you ? "orb--you" : ""}`} style={style}>
      {children ?? seat.name.slice(0, 1).toUpperCase()}
      {seat.badge && <span className="orb-badge">{seat.badge}</span>}
      {(seat.strikes ?? 0) > 0 && (
        <span className="orb-strike" title="Interruptions made">
          {seat.strikes}
        </span>
      )}
    </div>
  );
}

/** Seats around an ellipse. The viewer, if present, sits at the bottom. */
export function RoundTable({
  seats,
  centre,
}: {
  seats: SeatInfo[];
  centre?: ReactNode;
}) {
  const ordered = [...seats].sort((a, b) => Number(!!b.you) - Number(!!a.you));
  const n = ordered.length;
  return (
    <div className="table">
      <div className="table-centre">{centre}</div>
      {ordered.map((seat, index) => {
        // Index 0 (the viewer) at 90deg = bottom; the rest fan out clockwise.
        const angle = (Math.PI / 2 + (index * 2 * Math.PI) / Math.max(n, 1)) % (2 * Math.PI);
        const x = 50 + 43 * Math.cos(angle);
        const y = 50 + 40 * Math.sin(angle);
        return (
          <div
            key={seat.label}
            className={`seat ${seat.speaking ? "seat--speaking" : ""} ${seat.away ? "seat--away" : ""}`}
            style={{ left: `${x}%`, top: `${y}%` }}
          >
            <Orb seat={seat} />
            <div className="seat-name">
              {seat.name}
              {seat.you ? " (you)" : ""}
            </div>
            <div className="seat-sub">{seat.speaking ? "speaking" : seat.sub}</div>
          </div>
        );
      })}
    </div>
  );
}

export function RingClock({
  seconds,
  duration,
  warn,
}: {
  seconds: number;
  duration: number;
  warn?: boolean;
}) {
  const radius = 42;
  const circumference = 2 * Math.PI * radius;
  const fraction = duration > 0 ? Math.max(0, Math.min(1, seconds / duration)) : 0;
  const clock = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  return (
    <div className={`ring-clock ${warn ? "ring-clock--warn" : ""}`} role="timer" aria-label={clock}>
      <svg viewBox="0 0 100 100" aria-hidden>
        <circle className="track" cx="50" cy="50" r={radius} />
        <circle
          className="fill"
          cx="50"
          cy="50"
          r={radius}
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - fraction)}
        />
      </svg>
      <span>{clock}</span>
    </div>
  );
}

export function Notices({
  notices,
  nameOf,
}: {
  notices: TurnNotice[];
  nameOf: (label: string) => string;
}) {
  // Newest few only; each fades out of the list as newer ones push it down.
  return (
    <div className="notices" aria-live="polite">
      {notices.slice(0, 3).map((n) => (
        <div key={n.id} className={`notice notice--${n.kind}`}>
          {n.kind === "interruption" ? (
            <>
              <b>{nameOf(n.by)}</b> cut in on <b>{nameOf(n.target)}</b>
              <small>Interrupting costs points. Running penalty: −{n.penalty}</small>
            </>
          ) : (
            <>
              <b>{nameOf(n.by)}</b> replied to <b>{nameOf(n.target)}</b>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

export function ArenaHeader({
  topic,
  phaseLabel,
  live,
  seconds,
  duration,
  warn,
  right,
}: {
  topic: string;
  phaseLabel: string;
  live: boolean;
  seconds: number;
  duration: number;
  warn?: boolean;
  right?: ReactNode;
}) {
  return (
    <header className="arena-head">
      <div className="arena-topic">
        <span className={`arena-phase ${live ? "arena-phase--live" : ""}`}>
          <i />
          {phaseLabel}
        </span>
        <h1>{topic}</h1>
      </div>
      <div className="row" style={{ gap: "1rem" }}>
        {right}
        <RingClock seconds={seconds} duration={duration} warn={warn} />
      </div>
    </header>
  );
}
