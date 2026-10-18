"use client";

/**
 * `/platform?candidate=…&name=…&next=…` — the way in from the AI Interviewer
 * platform (M14 Day 4).
 *
 * The shell frames this page with the student it has signed in. GD exchanges
 * that for its own tokens (`POST /api/v1/auth/platform`) and moves on, so the
 * student never sees GD's sign-in, and a second student in the same browser
 * replaces the first rather than inheriting their account.
 */

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { platformSignIn, tokenStore } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { markEmbedded } from "@/lib/embedded";

const DEFAULT_NEXT = "/practice/new";

/** Only a path inside GD; anything else would make this an open redirect. */
function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : DEFAULT_NEXT;
}

function PlatformSignIn() {
  const params = useSearchParams();
  const router = useRouter();
  const { refreshUser } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // StrictMode runs effects twice in development
    started.current = true;
    const candidate = (params.get("candidate") ?? "").trim();
    if (!candidate) {
      setError("The platform did not say who you are. Open group discussion from its menu.");
      return;
    }
    markEmbedded();
    tokenStore.clear();
    platformSignIn(candidate, params.get("name") ?? "")
      .then(async (pair) => {
        tokenStore.save(pair);
        await refreshUser();
        router.replace(safeNext(params.get("next")));
      })
      .catch((exc: Error) => {
        setError(
          exc.message.includes("not enabled")
            ? "Group discussion is running on its own here, not as part of the platform."
            : "Group discussion could not sign you in. Reload to try again.",
        );
      });
  }, [params, refreshUser, router]);

  return (
    <div className="card" style={{ maxWidth: "32rem", margin: "4rem auto" }}>
      {error ? (
        <>
          <h1>Group discussion did not open</h1>
          <p className="meta">{error}</p>
        </>
      ) : (
        <p className="meta">Opening group discussion…</p>
      )}
    </div>
  );
}

export default function PlatformPage() {
  return (
    <Suspense fallback={null}>
      <PlatformSignIn />
    </Suspense>
  );
}
