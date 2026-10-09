"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { notifications } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { Icon, type IconName } from "./icons";

// Polling, not a socket: the control-plane socket is scoped to one session, and
// a notification can arrive when the user is not in one (FR-32).
const POLL_MS = 30_000;

const MAIN_NAV: { href: string; label: string; icon: IconName; match: (p: string) => boolean }[] = [
  {
    href: "/",
    label: "Group Discussion",
    icon: "users",
    match: (p) => p === "/" || p.startsWith("/sessions") || p.startsWith("/lobby/") || p.startsWith("/room"),
  },
  { href: "/lobbies", label: "Lobbies", icon: "grid", match: (p) => p === "/lobbies" },
  { href: "/practice/new", label: "AI practice", icon: "bot", match: (p) => p.startsWith("/practice") },
  {
    href: "/history",
    label: "Past sessions",
    icon: "history",
    match: (p) => p === "/history" || p.startsWith("/report"),
  },
];

export function Sidebar() {
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

  const brand = (
    <Link href="/" className="brand">
      <span className="logo-mark" aria-hidden>
        GD
      </span>
      <span>GD Platform</span>
    </Link>
  );

  if (loading) return <header className="guestbar">{brand}</header>;

  if (!user) {
    return (
      <header className="guestbar">
        {brand}
        <nav className="guestbar-nav">
          <Link href="/login" className={`nav-link ${pathname === "/login" ? "nav-link--active" : ""}`}>
            Sign in
          </Link>
          <Link href="/register">
            <button type="button">Get started</button>
          </Link>
        </nav>
      </header>
    );
  }

  const item = (href: string, label: string, icon: IconName, active: boolean, badge?: number) => (
    <Link
      key={href}
      href={href}
      className={`side-link ${active ? "side-link--active" : ""}`}
      aria-current={active ? "page" : undefined}
    >
      <Icon name={icon} className="nav-icon" />
      <span>{label}</span>
      {badge ? (
        <span className="count" aria-label={`${badge} unread`}>
          {badge}
        </span>
      ) : null}
    </Link>
  );

  const initial = (user.full_name || user.email)[0].toUpperCase();

  return (
    <aside className="sidebar">
      {brand}
      <nav className="side-nav" aria-label="Main">
        {MAIN_NAV.map((n) => item(n.href, n.label, n.icon, n.match(pathname)))}
      </nav>
      <nav className="side-nav side-nav--lower" aria-label="Account">
        {item("/notifications", "Notifications", "bell", pathname === "/notifications", unread)}
        {item("/profile", "Profile", "user", pathname === "/profile")}
      </nav>
      <div className="side-user">
        <span className="avatar">{initial}</span>
        <span className="side-user-name">
          {user.full_name}
          <span className="meta">{user.role}</span>
        </span>
        <button type="button" className="secondary side-signout" onClick={handleSignOut}>
          Sign out
        </button>
      </div>
    </aside>
  );
}
