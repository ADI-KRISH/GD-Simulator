"use client";

import Link from "next/link";  // client-side navigation link
import { usePathname, useRouter } from "next/navigation";  // current path and sign-out redirect
import { useEffect, useState } from "react";  // state and effects

import { notifications } from "@/lib/api";  // poll the unread count
import { useAuth } from "@/lib/auth-context";  // current user and sign-out
import { isEmbedded } from "@/lib/embedded";  // detect the platform shell
import { Icon, type IconName } from "./icons";
import { ThemeToggle } from "./theme-toggle";  // light / dark switch

// Polling, not a socket: the control-plane socket is scoped to one session, and
// a notification can arrive when the user is not in one (FR-32).
const POLL_MS = 30_000;

/** Primary navigation entries; `match` marks which paths highlight each one. */
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

/**
 * Left navigation: brand, links, notifications badge and account; slimmed down inside the
 * platform shell.
 */
export function Sidebar() {
  const { user, loading, signOut } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [unread, setUnread] = useState(0);
  // Read after mount: the server cannot know, and both renders must agree.
  const [embedded, setEmbedded] = useState(false);
  useEffect(() => setEmbedded(isEmbedded()), [pathname]);

  useEffect(() => {
    if (!user) {
      setUnread(0);
      return;
    }
    let cancelled = false;
    /** Refresh the unread-notifications badge. */
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

  /** Sign out and go to the login page. */
  async function handleSignOut() {
    await signOut();
    router.push("/login");
  }

  // The logo link, reused in every layout below.
  const brand = (
    <Link href="/" className="brand">
      <span className="logo-mark" aria-hidden>
        GD
      </span>
      <span>GD Platform</span>
    </Link>
  );

  // Inside the platform: its tabs only. The shell owns the brand, the account
  // and signing out, and the student came in signed in.
  if (embedded) {
    return (
      <header className="guestbar guestbar--embedded">
        <nav className="guestbar-nav" aria-label="Group discussion">
          {user &&
            MAIN_NAV.map((n) => (
              <Link
                key={n.href}
                href={n.href}
                className={`nav-link ${n.match(pathname) ? "nav-link--active" : ""}`}
                aria-current={n.match(pathname) ? "page" : undefined}
              >
                {n.label}
              </Link>
            ))}
        </nav>
      </header>
    );
  }

  if (loading) return <header className="guestbar">{brand}</header>;

  if (!user) {
    return (
      <header className="guestbar">
        {brand}
        <nav className="guestbar-nav">
          <ThemeToggle />
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

  /** One sidebar link, optionally with an unread-count badge. */
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
      {user.role !== "candidate" && (
        <nav className="side-nav" aria-label="Developer">
          {item("/observability", "Observability", "chart", pathname.startsWith("/observability"))}
        </nav>
      )}
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
        <ThemeToggle />
        <button type="button" className="secondary side-signout" onClick={handleSignOut}>
          Sign out
        </button>
      </div>
    </aside>
  );
}
