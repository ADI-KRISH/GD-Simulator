"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, sessions } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { Difficulty, GDType } from "@/lib/types";

/**
 * FR-03 — GD creation.
 *
 * The GD type is not cosmetic: the Supervisor picks which evaluator agents run
 * from it (design §22), so the form says so rather than presenting it as a
 * label.
 */
const GD_TYPES: { value: GDType; label: string; agents: string }[] = [
  {
    value: "general",
    label: "General",
    agents: "Content, Communication, Leadership, Collaboration, Behaviour",
  },
  {
    value: "technical",
    label: "Technical",
    agents: "Content, Critical Thinking, Communication, Collaboration, Behaviour",
  },
  {
    value: "leadership",
    label: "Leadership",
    agents: "Leadership, Communication, Collaboration, Behaviour",
  },
  {
    value: "hr_social",
    label: "HR / Social",
    agents: "Communication, Collaboration, Leadership, Behaviour, Critical Thinking",
  },
];

export default function NewSessionPage() {
  const router = useRouter();
  const { user, loading } = useAuth();
  const [form, setForm] = useState({
    topic: "",
    topic_category: "general",
    gd_type: "general" as GDType,
    difficulty: "intermediate" as Difficulty,
    min_participants: 3,
    max_participants: 8,
    duration_minutes: 15,
    preparation_minutes: 2,
    video_required: true,
    recording_enabled: false,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to create a discussion.</p>;
  if (user.role === "candidate") {
    return (
      <div className="card">
        <h1>Hosts only</h1>
        <p className="subtitle">
          Your account is a candidate account, so it can join discussions but not create them.
        </p>
      </div>
    );
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await sessions.create({
        topic: form.topic,
        topic_category: form.topic_category,
        gd_type: form.gd_type,
        difficulty: form.difficulty,
        min_participants: Number(form.min_participants),
        max_participants: Number(form.max_participants),
        duration_seconds: Number(form.duration_minutes) * 60,
        preparation_seconds: Number(form.preparation_minutes) * 60,
        video_required: form.video_required,
        recording_enabled: form.recording_enabled,
      });
      router.push(`/lobby/${created.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not create the discussion");
      setBusy(false);
    }
  }

  const selected = GD_TYPES.find((t) => t.value === form.gd_type);

  return (
    <div className="card">
      <h1>New group discussion</h1>
      <p className="subtitle">
        Candidates join with the code this creates. Nothing starts until the readiness condition
        is met.
      </p>

      {error && <div className="alert alert--error">{error}</div>}

      <form onSubmit={handleSubmit}>
        <div className="field">
          <label htmlFor="topic">Topic</label>
          <textarea
            id="topic"
            value={form.topic}
            onChange={(e) => setForm({ ...form, topic: e.target.value })}
            placeholder="Should governments regulate generative AI?"
            minLength={5}
            required
          />
        </div>

        <div className="grid">
          <div className="field">
            <label htmlFor="gd_type">Discussion type</label>
            <select
              id="gd_type"
              value={form.gd_type}
              onChange={(e) => setForm({ ...form, gd_type: e.target.value as GDType })}
            >
              {GD_TYPES.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="difficulty">Difficulty</label>
            <select
              id="difficulty"
              value={form.difficulty}
              onChange={(e) => setForm({ ...form, difficulty: e.target.value as Difficulty })}
            >
              <option value="beginner">Beginner</option>
              <option value="intermediate">Intermediate</option>
              <option value="advanced">Advanced</option>
            </select>
          </div>

          <div className="field">
            <label htmlFor="category">Category</label>
            <input
              id="category"
              value={form.topic_category}
              onChange={(e) => setForm({ ...form, topic_category: e.target.value })}
            />
          </div>
        </div>

        {selected && (
          <p className="meta" style={{ marginTop: "-0.5rem", marginBottom: "1rem" }}>
            Evaluators for this type: {selected.agents}.
          </p>
        )}

        <div className="grid">
          <div className="field">
            <label htmlFor="min">Minimum participants</label>
            <input
              id="min"
              type="number"
              min={2}
              max={20}
              value={form.min_participants}
              onChange={(e) => setForm({ ...form, min_participants: Number(e.target.value) })}
            />
          </div>
          <div className="field">
            <label htmlFor="max">Maximum participants</label>
            <input
              id="max"
              type="number"
              min={2}
              max={20}
              value={form.max_participants}
              onChange={(e) => setForm({ ...form, max_participants: Number(e.target.value) })}
            />
          </div>
          <div className="field">
            <label htmlFor="duration">Discussion length (minutes)</label>
            <input
              id="duration"
              type="number"
              min={1}
              max={120}
              value={form.duration_minutes}
              onChange={(e) => setForm({ ...form, duration_minutes: Number(e.target.value) })}
            />
          </div>
          <div className="field">
            <label htmlFor="prep">Preparation time (minutes)</label>
            <input
              id="prep"
              type="number"
              min={0}
              max={30}
              value={form.preparation_minutes}
              onChange={(e) => setForm({ ...form, preparation_minutes: Number(e.target.value) })}
            />
          </div>
        </div>

        <div className="row row--wrap" style={{ marginBottom: "1.25rem" }}>
          <label className="row" style={{ marginBottom: 0 }}>
            <input
              type="checkbox"
              checked={form.video_required}
              onChange={(e) => setForm({ ...form, video_required: e.target.checked })}
              style={{ width: "auto" }}
            />
            <span>Require video</span>
          </label>
          <label className="row" style={{ marginBottom: 0 }}>
            <input
              type="checkbox"
              checked={form.recording_enabled}
              onChange={(e) => setForm({ ...form, recording_enabled: e.target.checked })}
              style={{ width: "auto" }}
            />
            <span>Keep recordings</span>
          </label>
        </div>

        <button type="submit" disabled={busy}>
          {busy ? "Creating…" : "Create discussion"}
        </button>
      </form>

      <p className="footnote">
        Scoring uses the default rubric: Content 30%, Communication 20%, Leadership 20%,
        Collaboration 20%, Behaviour 10%. The final score is computed in code from the agents&rsquo;
        per-dimension scores.
      </p>
    </div>
  );
}
