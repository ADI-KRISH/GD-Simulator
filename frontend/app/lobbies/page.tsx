"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, lobbies } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { LobbyListItem } from "@/lib/types";

const REFRESH_MS = 5000;

export default function LobbiesPage() {
  const router = useRouter();
  const { user, loading } = useAuth();
  const [items, setItems] = useState<LobbyListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [joining, setJoining] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await lobbies.browse());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the lobbies.");
    }
  }, []);

  useEffect(() => {
    if (!user) return;
    void load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [user, load]);

  async function join(item: LobbyListItem) {
    setJoining(item.code);
    setError(null);
    try {
      const lobby = await lobbies.join(item.code);
      router.push(`/lobby/${lobby.session_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not join this lobby.");
      setJoining(null);
    }
  }

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to see open lobbies.</p>;

  return (
    <div className="arena" style={{ gridTemplateRows: "auto 1fr" }}>
      <header className="arena-head">
        <div className="arena-topic">
          <h1>Open lobbies</h1>
          <p className="arena-hint" style={{ textAlign: "left" }}>
            Join a discussion another student started, or open your own and set the topic and
            timer. You can sit in one discussion at a time.
          </p>
        </div>
        <Link href="/sessions/new">
          <button type="button" className="arena-btn arena-btn--primary">
            Open a lobby
          </button>
        </Link>
      </header>

      <div>
        {error && <div className="alert alert--error">{error}</div>}
        {items === null ? (
          <p className="arena-hint">Looking for lobbies…</p>
        ) : items.length === 0 ? (
          <div className="arena-card" style={{ textAlign: "center" }}>
            <p style={{ margin: "0 0 0.8rem" }}>Nobody is waiting for players right now.</p>
            <Link href="/sessions/new">
              <button type="button" className="arena-btn arena-btn--primary">
                Open the first lobby
              </button>
            </Link>
          </div>
        ) : (
          <div className="lobby-grid">
            {items.map((item) => (
              <div key={item.session_id} className="lobby-card">
                <div className="row row--between">
                  <span className="meta">
                    {item.difficulty}, {item.gd_type.replace("_", " ")}
                  </span>
                  <code>{item.code}</code>
                </div>
                <strong className="lobby-topic">{item.topic}</strong>
                <div className="meta">
                  Hosted by {item.host_name}. {Math.round(item.duration_seconds / 60)} min
                  {item.preparation_seconds > 0 &&
                    ` plus ${Math.round(item.preparation_seconds / 60)} min preparation`}
                </div>
                <div className="row row--between">
                  <span className="seats" aria-label={`${item.occupancy} of ${item.capacity} seats taken`}>
                    {Array.from({ length: item.capacity }, (_, i) => (
                      <i key={i} className={i < item.occupancy ? "seats-on" : ""} />
                    ))}
                  </span>
                  <button
                    type="button"
                    className="arena-btn"
                    onClick={() => void join(item)}
                    disabled={joining !== null}
                  >
                    {joining === item.code ? "Joining…" : "Join"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
