"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Icon } from "@/app/icons";
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
    <div className="stack" style={{ gap: "1.25rem" }}>
      <header className="page-head page-head--row">
        <div>
          <h1>Open lobbies</h1>
          <p>
            Join a discussion another student started, or open your own. You can sit in one
            discussion at a time.
          </p>
        </div>
        <Link href="/sessions/new">
          <button type="button">
            <Icon name="plus" size={16} /> Create lobby
          </button>
        </Link>
      </header>

      {error && <div className="alert alert--error">{error}</div>}
      {items === null ? (
        <p className="empty">Looking for lobbies…</p>
      ) : items.length === 0 ? (
        <div className="card empty-state">
          <span className="tile-icon tile-icon--lg" aria-hidden>
            <Icon name="grid" size={26} />
          </span>
          <h2>Nobody is waiting for players right now</h2>
          <p className="meta">Open the first lobby and share its code with your friends.</p>
          <Link href="/sessions/new">
            <button type="button">Create lobby</button>
          </Link>
        </div>
      ) : (
        <div className="lobby-grid">
          {items.map((item) => (
            <div key={item.session_id} className="lobby-card">
              <div className="row row--between">
                <span className="chip chip--info">{item.gd_type.replace("_", " ")}</span>
                <code>{item.code}</code>
              </div>
              <strong className="lobby-topic">{item.topic}</strong>
              <ul className="chips-row chips-row--tight">
                <li>
                  <Icon name="users" size={15} /> {item.host_name}
                </li>
                <li>
                  <Icon name="clock" size={15} /> {Math.round(item.duration_seconds / 60)} min
                  {item.preparation_seconds > 0 &&
                    `, plus ${Math.round(item.preparation_seconds / 60)} to prepare`}
                </li>
                <li className="cap">
                  <Icon name="target" size={15} /> {item.difficulty}
                </li>
              </ul>
              <div className="row row--between">
                <span
                  className="seats"
                  aria-label={`${item.occupancy} of ${item.capacity} seats taken`}
                >
                  {Array.from({ length: item.capacity }, (_, i) => (
                    <i key={i} className={i < item.occupancy ? "seats-on" : ""} />
                  ))}
                </span>
                <button
                  type="button"
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
  );
}
