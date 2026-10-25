"use client";

/** FR-32 — notifications. */

import Link from "next/link";  // client-side navigation link
import { useCallback, useEffect, useState } from "react";  // state and effects

import { ApiError, notifications } from "@/lib/api";  // notification calls and error type
import { useAuth } from "@/lib/auth-context";  // require sign-in
import type { NotificationItem, NotificationKind } from "@/lib/types";  // notification types

/** Short label shown for each notification kind. */
const KIND_LABELS: Record<NotificationKind, string> = {
  lobby_created: "Lobby",
  session_invitation: "Invitation",
  session_starting: "Starting",
  session_ended: "Ended",
  evaluation_completed: "Evaluation",
  report_available: "Report",
};

/** The notification inbox with mark-read actions. */
export default function NotificationsPage() {
  const { user, loading } = useAuth();
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [error, setError] = useState<string | null>(null);

  /** Fetch notifications and the unread count. */
  const load = useCallback(async () => {
    if (!user) return;
    try {
      const list = await notifications.list();
      setItems(list.items);
      setUnread(list.unread);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not load notifications");
    }
  }, [user]);

  useEffect(() => {
    void load();
  }, [load]);

  /** Mark one notification read, then reload. */
  async function markRead(id: string) {
    await notifications.markRead(id).catch(() => undefined);
    await load();
  }

  /** Mark everything read, then reload. */
  async function markAllRead() {
    await notifications.markAllRead().catch(() => undefined);
    await load();
  }

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to see your notifications.</p>;

  return (
    <div className="card">
      <div className="row row--between" style={{ marginBottom: "1rem" }}>
        <div>
          <h1>Notifications</h1>
          <p className="subtitle" style={{ marginBottom: 0 }}>
            {unread > 0 ? `${unread} unread` : "All caught up"}
          </p>
        </div>
        {unread > 0 && (
          <button type="button" className="secondary" onClick={markAllRead}>
            Mark all read
          </button>
        )}
      </div>

      {error && <div className="alert alert--error">{error}</div>}

      {items.length === 0 ? (
        <p className="empty">Nothing yet.</p>
      ) : (
        <div className="stack" style={{ gap: "0.6rem" }}>
          {items.map((item) => (
            <div
              key={item.id}
              className="participant"
              style={{ opacity: item.read_at ? 0.6 : 1 }}
            >
              <div className="row row--between row--wrap">
                <div style={{ flex: "1 1 20rem" }}>
                  <div className="row" style={{ marginBottom: "0.2rem" }}>
                    <span className="pill">{KIND_LABELS[item.kind] ?? item.kind}</span>
                    <strong style={{ fontSize: "0.92rem" }}>{item.title}</strong>
                  </div>
                  <p className="meta" style={{ margin: 0 }}>
                    {item.body}
                  </p>
                </div>
                <div className="row">
                  {item.link && <Link href={item.link}>Open</Link>}
                  {!item.read_at && (
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => markRead(item.id)}
                      style={{ padding: "0.25rem 0.55rem", fontSize: "0.8rem" }}
                    >
                      Mark read
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
