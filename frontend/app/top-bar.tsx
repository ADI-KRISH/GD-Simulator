"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { notifications } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

// Polling, not a socket: the control-plane socket is scoped to one session, and
// a notification can arrive when the user is not in one (FR-32).
const POLL_MS = 30_000;

export function TopBar() {
  const { user, loading, signOut } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    if (!user) {
      setUnread(0);
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const list = await notifications.list(true);
        if (!cancelled) setUnread(list.unread);
      } catch {
        // A failed poll is not worth surfacing in the chrome.
      }
    };
    void poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [user]);

  async function handleSignOut() {
    await signOut();
    router.push("/login");
  }

  const link = (href: string, label: React.ReactNode) => (
    <Link
      href={href}
      className={`nav-link ${pathname === href ? "nav-link--active" : ""}`}
      aria-current={pathname === href ? "page" : undefined}
    >
      {label}
    </Link>
  );

  return (
    <header className="topbar">
      <Link href="/" style={{ color: "inherit", textDecoration: "none" }}>
        <span className="brand">
          <span className="logo-mark" aria-hidden>
            GD
          </span>
          <span>
            GD Platform
            <small className="meta">AI-evaluated group discussions</small>
          </span>
        </span>
      </Link>

      <nav className="nav">
        {loading ? null : user ? (
          <>
            {link("/", "Home")}
            {user.role !== "candidate" && link("/sessions/new", "New GD")}
            {link("/history", "Performance")}
            {link(
              "/notifications",
              <>
                Notifications
                {unread > 0 && (
                  <span className="count" aria-label={`${unread} unread`}>
                    {unread}
                  </span>
                )}
              </>,
            )}
            <span className="user-chip">
              <Link href="/profile" title="Profile" style={{ textDecoration: "none" }}>
                <span className="avatar">{(user.full_name || user.email)[0].toUpperCase()}</span>
              </Link>
              <span className="user-chip-name">
                {user.full_name}
                <span className="meta" style={{ display: "block", fontWeight: 400 }}>
                  {user.role}
                </span>
              </span>
              <button className="secondary" onClick={handleSignOut}>
                Sign out
              </button>
            </span>
          </>
        ) : (
          <>
            {link("/login", "Sign in")}
            <Link href="/register">
              <button type="button">Get started</button>
            </Link>
          </>
        )}
      </nav>
    </header>
  );
}
