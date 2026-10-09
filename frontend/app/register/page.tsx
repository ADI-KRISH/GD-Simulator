"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Select } from "@/app/select";

import { ApiError, request, tokenStore, type TokenPair } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function RegisterPage() {
  const router = useRouter();
  const { refreshUser } = useAuth();
  const [form, setForm] = useState({
    full_name: "",
    email: "",
    password: "",
    role: "candidate" as "candidate" | "host",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function update(field: keyof typeof form) {
    return (event: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [field]: event.target.value }));
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // `role` is part of the register contract but not of the typed helper's
      // narrower signature, so this goes through `request` directly.
      await request<unknown>("/api/v1/auth/register", { method: "POST", body: form });
      const pair = await request<TokenPair>("/api/v1/auth/login", {
        method: "POST",
        body: { email: form.email, password: form.password },
      });
      tokenStore.save(pair);
      await refreshUser();
      router.push("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not create the account");
      setBusy(false);
    }
  }

  return (
    <div className="card card--narrow">
      <h1>Create an account</h1>
      <p className="subtitle">Hosts create discussions; candidates join and are evaluated.</p>

      {error && <div className="alert alert--error">{error}</div>}

      <form onSubmit={handleSubmit}>
        <div className="field">
          <label htmlFor="full_name">Full name</label>
          <input id="full_name" value={form.full_name} onChange={update("full_name")} required />
        </div>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            value={form.email}
            onChange={update("email")}
            autoComplete="email"
            required
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            value={form.password}
            onChange={update("password")}
            autoComplete="new-password"
            minLength={12}
            required
          />
          <p className="meta" style={{ marginTop: "0.3rem" }}>
            At least 12 characters.
          </p>
        </div>
        <div className="field">
          <label htmlFor="role">Account type</label>
          <Select<"candidate" | "host">
            id="role"
            value={form.role}
            onChange={(role) => setForm((prev) => ({ ...prev, role }))}
            options={[
              { value: "candidate", label: "Candidate: join discussions" },
              { value: "host", label: "Host: create discussions" },
            ]}
          />
        </div>
        <button type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "Creating…" : "Create account"}
        </button>
      </form>

      <p className="footnote">
        Already registered? <Link href="/login">Sign in</Link>
      </p>
    </div>
  );
}
