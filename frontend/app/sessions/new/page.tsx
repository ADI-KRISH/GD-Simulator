"use client";

import Link from "next/link";  // client-side navigation link
import { useRouter } from "next/navigation";  // go to the lobby after creating
import { useState } from "react";  // form state

import { Icon } from "@/app/icons";  // icon set
import { Select } from "@/app/select";  // styled dropdown
import { ApiError, sessions } from "@/lib/api";  // create the session
import { useAuth } from "@/lib/auth-context";  // require sign-in
import type { Difficulty, GDType } from "@/lib/types";  // difficulty and GD type

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

/** Suggested topics for the "Suggest a topic" button. */
const TOPIC_IDEAS = [
  "Should remote work be the future of the tech industry?",
  "Should governments regulate generative AI?",
  "Is a four-day work week good for productivity?",
  "Should social media platforms verify every user's identity?",
  "Do electric vehicles solve the climate problem or move it elsewhere?",
  "Should coding be a compulsory school subject?",
  "Is startup culture glorifying burnout?",
  "Should companies be allowed to monitor employees' work devices?",
  "Can online degrees replace campus education?",
  "Should there be a cap on gig workers' platform commissions?",
];

/** Turn minute values into Select options. */
const minutes = (values: number[]) => values.map((m) => ({ value: m, label: `${m} minutes` }));
/** Turn participant counts into Select options. */
const counts = (values: number[]) => values.map((n) => ({ value: n, label: `${n}` }));

/** Form to create a group discussion lobby (topic, timing, size, type, difficulty). */
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
  const [ideaIndex, setIdeaIndex] = useState(-1);

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to create a discussion.</p>;

  /** Fill the topic box with a random idea (never the same one twice in a row). */
  function suggestTopic() {
    const next = (ideaIndex + 1 + Math.floor(Math.random() * (TOPIC_IDEAS.length - 1))) % TOPIC_IDEAS.length;
    setIdeaIndex(next);
    setForm((prev) => ({ ...prev, topic: TOPIC_IDEAS[next] }));
  }

  /** Create the session and open its lobby. */
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
  const hostName = user.full_name.split(" ")[0];
  const durationOptions = minutes(
    [5, 10, 15, 20, 30, 45, 60].includes(form.duration_minutes)
      ? [5, 10, 15, 20, 30, 45, 60]
      : [form.duration_minutes, 5, 10, 15, 20, 30, 45, 60].sort((a, b) => a - b),
  );

  return (
    <form onSubmit={handleSubmit} className="stack" style={{ gap: "1.25rem" }}>
      <header className="page-head page-head--back">
        <Link href="/" className="back" aria-label="Back to home">
          <Icon name="arrow-left" />
        </Link>
        <div>
          <h1>Create GD lobby</h1>
          <p>Set up your discussion and invite your friends.</p>
        </div>
      </header>

      {error && <div className="alert alert--error">{error}</div>}

      <div className="create-layout">
        <div className="stack" style={{ gap: "1.25rem" }}>
          <section className="card">
            <div className="row row--between" style={{ marginBottom: "0.7rem" }}>
              <h2 className="section-title" style={{ margin: 0 }}>
                Discussion topic
              </h2>
              <button type="button" className="secondary button-sm" onClick={suggestTopic}>
                <Icon name="shuffle" size={16} /> Suggest a topic
              </button>
            </div>
            <textarea
              id="topic"
              aria-label="Discussion topic"
              value={form.topic}
              onChange={(e) => setForm({ ...form, topic: e.target.value })}
              placeholder="Should governments regulate generative AI?"
              rows={3}
              minLength={5}
              required
            />

            <div className="form-grid" style={{ marginTop: "1.1rem" }}>
              <div>
                <label htmlFor="duration">Discussion length</label>
                <Select<number>
                  id="duration"
                  value={form.duration_minutes}
                  onChange={(duration_minutes) => setForm({ ...form, duration_minutes })}
                  options={durationOptions}
                />
              </div>
              <div>
                <label htmlFor="prep">Preparation time</label>
                <Select<number>
                  id="prep"
                  value={form.preparation_minutes}
                  onChange={(preparation_minutes) => setForm({ ...form, preparation_minutes })}
                  options={[0, 1, 2, 3, 5, 10].map((m) => ({
                    value: m,
                    label: m === 0 ? "No preparation" : `${m} minutes`,
                  }))}
                />
              </div>
              <div>
                <label htmlFor="min">Minimum participants</label>
                <Select<number>
                  id="min"
                  value={form.min_participants}
                  onChange={(min_participants) =>
                    setForm({
                      ...form,
                      min_participants,
                      max_participants: Math.max(form.max_participants, min_participants),
                    })
                  }
                  options={counts([2, 3, 4, 5, 6])}
                />
              </div>
              <div>
                <label htmlFor="max">Maximum participants</label>
                <Select<number>
                  id="max"
                  value={form.max_participants}
                  onChange={(max_participants) =>
                    setForm({
                      ...form,
                      max_participants,
                      min_participants: Math.min(form.min_participants, max_participants),
                    })
                  }
                  options={counts([3, 4, 5, 6, 7, 8, 10, 12])}
                />
              </div>
            </div>
          </section>

          <section className="card">
            <h2 className="section-title">Format</h2>
            <div className="form-grid">
              <div>
                <label htmlFor="gd_type">Discussion type</label>
                <Select<GDType>
                  id="gd_type"
                  value={form.gd_type}
                  onChange={(gd_type) => setForm({ ...form, gd_type })}
                  options={GD_TYPES.map((t) => ({ value: t.value, label: t.label }))}
                />
              </div>
              <div>
                <label htmlFor="difficulty">Difficulty</label>
                <Select<Difficulty>
                  id="difficulty"
                  value={form.difficulty}
                  onChange={(difficulty) => setForm({ ...form, difficulty })}
                  options={[
                    { value: "beginner", label: "Beginner" },
                    { value: "intermediate", label: "Intermediate" },
                    { value: "advanced", label: "Advanced" },
                  ]}
                />
              </div>
              <div>
                <label htmlFor="category">Category</label>
                <input
                  id="category"
                  value={form.topic_category}
                  onChange={(e) => setForm({ ...form, topic_category: e.target.value })}
                />
              </div>
            </div>
            {selected && (
              <p className="meta" style={{ margin: "0.8rem 0 0" }}>
                Evaluators for this type: {selected.agents}.
              </p>
            )}

            <div className="switches">
              <label className="switch-row">
                <span>
                  <strong>Require video</strong>
                  <small>Participants join with their camera on.</small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  checked={form.video_required}
                  onChange={(e) => setForm({ ...form, video_required: e.target.checked })}
                />
              </label>
              <label className="switch-row">
                <span>
                  <strong>Keep recordings</strong>
                  <small>Store the audio so the discussion can be reviewed later.</small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  checked={form.recording_enabled}
                  onChange={(e) => setForm({ ...form, recording_enabled: e.target.checked })}
                />
              </label>
            </div>
          </section>
        </div>

        <aside className="card summary">
          <span className="summary-avatar" aria-hidden>
            {hostName[0]?.toUpperCase()}
          </span>
          <h2>{hostName}&rsquo;s lobby</h2>
          <p className="meta">
            {form.topic.trim() ? form.topic : "Your topic will appear here."}
          </p>
          <ul className="summary-list">
            <li>
              <Icon name="clock" size={18} />
              {form.duration_minutes} minutes
              {form.preparation_minutes > 0 && `, plus ${form.preparation_minutes} to prepare`}
            </li>
            <li>
              <Icon name="users" size={18} />
              {form.min_participants} to {form.max_participants} participants
            </li>
            <li>
              <Icon name="lock" size={18} />
              Friends join with a code
            </li>
            <li>
              <Icon name="target" size={18} />
              {selected?.label}, {form.difficulty}
            </li>
          </ul>
          <button type="submit" className="button-lg" disabled={busy} style={{ width: "100%" }}>
            {busy ? "Creating…" : "Create lobby"}
          </button>
          <p className="footnote" style={{ marginTop: "0.9rem" }}>
            Scoring uses the default rubric: Content 30%, Communication 20%, Leadership 20%,
            Collaboration 20%, Behaviour 10%. The final score is computed in code from the
            agents&rsquo; per-dimension scores.
          </p>
        </aside>
      </div>
    </form>
  );
}
