"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, api } from "@/lib/api";

export default function ResetPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [status, setStatus] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleRequest(event: React.FormEvent) {
    event.preventDefault();
    setStatus(null);
    setBusy(true);
    try {
      const result = await api.requestPasswordReset(email);
      if (result.reset_token) {
        setToken(result.reset_token);
        setStatus({
          kind: "ok",
          text: "Reset token issued and filled in below. It is shown here because there is no mail service running locally.",
        });
      } else {
        setStatus({ kind: "ok", text: result.detail });
      }
    } catch (err) {
      setStatus({
        kind: "error",
        text: err instanceof ApiError ? err.message : "Could not reach the backend.",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm(event: React.FormEvent) {
    event.preventDefault();
    setStatus(null);
    setBusy(true);
    try {
      await api.confirmPasswordReset(token, newPassword);
      router.push("/login");
    } catch (err) {
      setStatus({
        kind: "error",
        text: err instanceof ApiError ? err.message : "Could not reach the backend.",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card card--narrow">
      <h1>Reset password</h1>
      <p className="subtitle">Request a token, then use it to set a new password.</p>

      {status && (
        <div className={status.kind === "ok" ? "alert alert--ok" : "alert alert--error"}>
          {status.text}
        </div>
      )}

      <form onSubmit={handleRequest}>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <button type="submit" className="secondary" disabled={busy}>
          Request reset token
        </button>
      </form>

      <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "1.5rem 0" }} />

      <form onSubmit={handleConfirm}>
        <div className="field">
          <label htmlFor="token">Reset token</label>
          <input id="token" required value={token} onChange={(e) => setToken(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="new_password">New password</label>
          <input
            id="new_password"
            type="password"
            required
            minLength={8}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
          />
        </div>
        <button type="submit" disabled={busy}>
          Set new password
        </button>
      </form>

      <p className="footnote">
        <Link href="/login">Back to sign in</Link>
      </p>
    </div>
  );
}
