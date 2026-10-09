"use client";

/** FR-02 — user profile. */

import { useState } from "react";

import { Select } from "@/app/select";

import { ApiError, api, type ExperienceLevel } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const LEVELS: { value: ExperienceLevel; label: string }[] = [
  { value: "student", label: "Student" },
  { value: "entry", label: "Entry level" },
  { value: "mid", label: "Mid level" },
  { value: "senior", label: "Senior" },
];

export default function ProfilePage() {
  const { user, loading, refreshUser } = useAuth();
  const [fullName, setFullName] = useState<string | null>(null);
  const [skills, setSkills] = useState<string | null>(null);
  const [level, setLevel] = useState<ExperienceLevel | null>(null);
  const [status, setStatus] = useState<{ ok: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to view your profile.</p>;

  // Null means "not edited yet", so the form shows the server's value without
  // needing an effect to seed it.
  const nameValue = fullName ?? user.full_name;
  const skillsValue = skills ?? (user.profile?.skills ?? []).join(", ");
  const levelValue = level ?? user.profile?.experience_level ?? "student";

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setStatus(null);
    try {
      await api.updateProfile({
        full_name: nameValue,
        skills: skillsValue
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        experience_level: levelValue,
      });
      await refreshUser();
      setStatus({ ok: true, message: "Profile saved." });
    } catch (err) {
      setStatus({
        ok: false,
        message: err instanceof ApiError ? err.message : "could not save the profile",
      });
    } finally {
      setBusy(false);
    }
  }

  const profile = user.profile;

  return (
    <div className="stack">
      <div className="card">
        <h1>Your profile</h1>
        <p className="subtitle">
          Signed in as {user.email} · {user.role}
        </p>

        {status && (
          <div className={`alert ${status.ok ? "alert--ok" : "alert--error"}`}>
            {status.message}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="full_name">Full name</label>
            <input id="full_name" value={nameValue} onChange={(e) => setFullName(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="skills">Skills (comma separated)</label>
            <input
              id="skills"
              value={skillsValue}
              onChange={(e) => setSkills(e.target.value)}
              placeholder="public speaking, policy, systems design"
            />
          </div>
          <div className="field">
            <label htmlFor="level">Experience level</label>
            <Select<ExperienceLevel>
              id="level"
              value={levelValue}
              onChange={setLevel}
              options={LEVELS}
            />
          </div>
          <button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save profile"}
          </button>
        </form>
      </div>

      {profile && (
        <div className="card">
          <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.9rem" }}>
            From your completed discussions
          </h2>
          <div className="grid">
            <div>
              <dt>Discussions completed</dt>
              <dd style={{ fontSize: "1.3rem" }}>{profile.sessions_completed}</dd>
            </div>
            <div>
              <dt>Strengths</dt>
              <dd>{profile.strengths.join(", ") || "—"}</dd>
            </div>
            <div>
              <dt>Areas to improve</dt>
              <dd>{profile.improvement_areas.join(", ") || "—"}</dd>
            </div>
          </div>
          <p className="footnote">
            These are derived from your evaluation history, not entered by hand.
          </p>
        </div>
      )}
    </div>
  );
}
