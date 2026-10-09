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

/**
 * Everyone in the room as a tile. There is no video here, so each tile is an
 * avatar; the one holding the floor gets a ring and live level bars. The caption
 * under the grid is whatever was said last.
 */
export function RoundTable({
  seats,
  centre,
}: {
  seats: SeatInfo[];
  centre?: ReactNode;
}) {
  const ordered = [...seats].sort((a, b) => Number(!!b.you) - Number(!!a.you));
  return (
    <div className="stage-wrap">
      <div className="tiles" data-count={Math.min(ordered.length, 6)}>
        {ordered.map((seat) => (
          <div
            key={seat.label}
            className={`vtile ${seat.speaking ? "vtile--speaking" : ""} ${seat.away ? "vtile--away" : ""}`}
            style={{ "--h": seat.hue ?? hueFor(seat.label) } as CSSProperties}
          >
            <Orb seat={seat} />
            <div className="vtile-label">
              {seat.speaking ? (
                <span className="eq" aria-label="speaking">
                  <i />
                  <i />
                  <i />
                </span>
              ) : (
                <span className="vtile-dot" aria-hidden />
              )}
              <span className="vtile-name">
                {seat.name}
                {seat.you ? " (you)" : ""}
              </span>
              {!seat.speaking && seat.sub && <small>{seat.sub}</small>}
            </div>
          </div>
        ))}
      </div>
      <div className="caption-bar">{centre}</div>
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
  const clock = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  const total = `${String(Math.floor(duration / 60)).padStart(2, "0")}:${String(Math.floor(duration % 60)).padStart(2, "0")}`;
  return (
    <header className="arena-head">
      <div className="arena-topic">
        <div className="arena-title">
          <h1>Group Discussion</h1>
          <span className={`arena-phase ${live ? "arena-phase--live" : ""}`}>
            <i />
            {phaseLabel}
          </span>
        </div>
        <p className="arena-sub">{topic}</p>
      </div>
      <div className="arena-tools">
        {right}
        <span
          className={`timer-pill ${warn ? "timer-pill--warn" : ""}`}
          role="timer"
          aria-label={`${clock} remaining of ${Math.round(duration / 60)} minutes`}
        >
          {live && <i className="rec" aria-hidden />}
          {clock}
          {duration > 0 && <small> / {total}</small>}
        </span>
      </div>
    </header>
  );
}
