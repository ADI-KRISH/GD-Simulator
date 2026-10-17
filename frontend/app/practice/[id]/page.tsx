"use client";

/**
 * The practice room: you at the bottom of a round table, AI personas around you.
 * Everything is spoken. The orbs swell with the voice that holds the floor.
 */

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  ArenaHeader,
  FloorBars,
  IdeasStrip,
  Notices,
  RoundTable,
  type SeatInfo,
} from "@/app/immersive/parts";
import { ApiError, sessions } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { usePractice } from "@/lib/use-practice";

const FINISHED = new Set(["ENDED", "EVALUATING", "COMPLETED"]);

export default function PracticeRoomPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { user } = useAuth();
  const room = usePractice(id ?? null);
  const [error, setError] = useState<string | null>(null);
  const [ending, setEnding] = useState(false);
  const [draft, setDraft] = useState("");
  const [handUp, setHandUp] = useState(false);

  // A raised hand shows as raised until the moderator has had a moment to invite.
  useEffect(() => {
    if (!handUp) return;
    const timer = setTimeout(() => setHandUp(false), 8000);
    return () => clearTimeout(timer);
  }, [handUp]);

  const state = room.sessionState ?? "";
  const live = state === "LIVE";
  const preparing = state === "STARTING";
  const notStarted = state === "WAITING" || state === "READY" || state === "";

  // Everyone moves to the report when the clock runs out.
  useEffect(() => {
    if (FINISHED.has(state)) router.push(`/report/${id}`);
  }, [state, id, router]);

  const personas = room.snapshot?.personas ?? [];
  const names = new Map<string, string>([["A", user?.full_name?.split(" ")[0] ?? "You"]]);
  for (const p of personas) names.set(p.label, p.name);
  const nameOf = (label: string) => names.get(label) ?? label;

  const seats: SeatInfo[] = [
    {
      label: "A",
      name: nameOf("A"),
      you: true,
      speaking: room.speaking.includes("A"),
      level: room.micLevel,
      strikes: room.penalties["A"]?.interruptions,
      sub: room.typing ? "typing" : room.muted ? "muted" : room.listening ? "listening" : "mic off",
    },
    ...personas.map((p) => ({
      label: p.label,
      name: p.name,
      badge: "AI",
      speaking: room.speaking.includes(p.label),
      level: room.speaking.includes(p.label) ? room.personaLevel : 0,
      sub: room.thinking === p.label ? "thinking…" : "listening",
    })),
  ];

  const last = room.captions[room.captions.length - 1];
  const penalty = room.penalties["A"]?.penalty ?? 0;
  const order = ["A", ...personas.map((p) => p.label)];

  function send(event: React.FormEvent) {
    event.preventDefault();
    if (room.sendText(draft)) setDraft("");
  }

  async function endNow() {
    setEnding(true);
    try {
      await sessions.end(id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not end the session.");
      setEnding(false);
    }
  }

  const topic = room.topic?.released ? room.topic.topic : "Your topic is revealed when preparation starts";
  const timer = room.timer;

  return (
    <div className="arena">
      <ArenaHeader
        topic={topic}
        phaseLabel={live ? "Live" : preparing ? "Preparation: gather your thoughts" : "Waiting to begin"}
        live={live}
        seconds={timer?.seconds_remaining ?? 0}
        duration={timer?.duration_seconds ?? room.snapshot?.duration_seconds ?? 0}
        warn={timer?.warning}
        right={
          live && penalty > 0 ? (
            <span className="arena-phase" title="Interruption penalty so far">
              <i style={{ background: "var(--hot)" }} />
              Interruptions: −{penalty}
            </span>
          ) : undefined
        }
      />

      <div>
        <RoundTable
          seats={seats}
          centre={
            notStarted ? (
              <p>Put on headphones, then start when you are ready.</p>
            ) : preparing ? (
              <p>Preparation. The personas will open the discussion when the clock starts.</p>
            ) : last ? (
              <>
                <p className="caption-who">{last.name}</p>
                <p className="caption-now">{last.text}</p>
              </>
            ) : (
              <p>The table is quiet. Someone will open in a moment.</p>
            )
          }
        />
        <Notices notices={room.notices} nameOf={nameOf} />
      </div>

      <div className="stack" style={{ gap: "0.7rem" }}>
        <IdeasStrip ideas={room.ideas} />
        {(live || Object.keys(room.floor).length > 0) && (
          <FloorBars floor={room.floor} order={order} nameOf={nameOf} />
        )}
        {(error || room.error || room.micError) && (
          <div className="alert alert--error" style={{ margin: "0 auto" }}>
            {error ?? room.micError ?? room.error}
            {room.micError && notStarted && (
              <button type="button" className="arena-btn" style={{ marginLeft: "0.8rem" }} onClick={room.startTyped}>
                Type instead
              </button>
            )}
          </div>
        )}
        {(preparing || live) && room.typing && (
          <form className="composer" onSubmit={send}>
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={live ? "Type your point and press Enter" : "The discussion opens when the clock starts"}
              aria-label="Your turn"
              maxLength={600}
              disabled={!live}
            />
            <button type="submit" className="arena-btn arena-btn--primary" disabled={!live || !draft.trim()}>
              Send
            </button>
          </form>
        )}
        <div className="arena-foot">
          {notStarted && (
            <>
              <button type="button" className="arena-btn arena-btn--primary" onClick={() => void room.start()} disabled={!room.connected}>
                {room.connected ? "Allow microphone and start" : "Connecting…"}
              </button>
              <button type="button" className="arena-btn" onClick={room.startTyped} disabled={!room.connected}>
                Type instead
              </button>
            </>
          )}
          {(preparing || live) && (
            <>
              {!room.typing && (
                <>
                  <button type="button" className="arena-btn" onClick={room.toggleMute} aria-pressed={room.muted}>
                    {room.muted ? "Unmute" : "Mute"}
                  </button>
                  <div className="mic-meter" aria-hidden style={{ ["--lvl" as string]: room.muted ? 0 : room.micLevel }}>
                    <span />
                  </div>
                </>
              )}
              {live && (
                <button
                  type="button"
                  className="arena-btn"
                  aria-pressed={handUp}
                  onClick={() => {
                    room.raiseHand();
                    setHandUp(true);
                  }}
                  disabled={handUp}
                >
                  {handUp ? "Hand raised" : "Raise hand"}
                </button>
              )}
              <button type="button" className="arena-btn arena-btn--danger" onClick={endNow} disabled={ending}>
                {ending ? "Ending…" : "Finish and get my report"}
              </button>
            </>
          )}
        </div>
        {live && (
          <p className="arena-hint" style={{ margin: "0 auto" }}>
            {room.typing
              ? "Type when you have a point. It goes in when the current speaker finishes — typing never interrupts anyone."
              : "Speak when you have a point. If you talk over a persona they stop, and it counts as an interruption."}{" "}
            Real GDs have no hand-raise; use it to practise, then try coming in on your own.
          </p>
        )}
        {room.captions.length > 0 && (
          <div className="captions" aria-label="Transcript">
            {room.captions.slice(-6).map((c) => (
              <div key={c.id}>
                <b>{c.name}:</b> {c.text}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
