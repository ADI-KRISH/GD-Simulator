"use client";

import { useRouter } from "next/navigation";  // go to the room once created
import { useEffect, useState } from "react";  // state and effects

import { Orb, hueFor } from "@/app/immersive/parts";  // persona avatars
import { Select } from "@/app/select";  // styled dropdown
import { ApiError, practice } from "@/lib/api";  // persona list and session creation
import { useAuth } from "@/lib/auth-context";  // require sign-in
import type { PersonaPreset } from "@/lib/types";  // persona preset type

/** The most personas allowed at the table. */
const MAX_PERSONAS = 4;

/** Set up a practice session: topic, length, difficulty and which AI personas join. */
export default function NewPracticePage() {
  const router = useRouter();
  const { user, loading } = useAuth();
  const [presets, setPresets] = useState<PersonaPreset[]>([]);
  const [voice, setVoice] = useState<"elevenlabs" | "browser">("browser");
  const [chosen, setChosen] = useState<string[]>(["arjun", "bhavana"]);
  const [topic, setTopic] = useState("");
  const [minutes, setMinutes] = useState(5);
  const [difficulty, setDifficulty] = useState("intermediate");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!user) return;
    practice
      .personas()
      .then((res) => {
        setPresets(res.personas);
        setVoice(res.voice);
      })
      .catch(() => setError("Could not load the personas. Is the backend running?"));
  }, [user]);

  if (loading) return <p className="empty">Loading…</p>;
  if (!user) return <p className="empty">Sign in to practice.</p>;

  /** Add or remove a persona from the table (capped at MAX_PERSONAS). */
  function toggle(key: string) {
    setChosen((prev) =>
      prev.includes(key)
        ? prev.filter((k) => k !== key)
        : prev.length < MAX_PERSONAS
          ? [...prev, key]
          : prev,
    );
  }

  /** Create the practice session and enter its room. */
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await practice.create({
        topic,
        difficulty,
        duration_seconds: minutes * 60,
        preparation_seconds: 20,
        personas: chosen.map((preset) => ({ preset })),
      });
      router.push(`/practice/${created.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the practice session.");
      setBusy(false);
    }
  }

  return (
    <div className="arena" style={{ gridTemplateRows: "auto" }}>
      <form className="arena-card" onSubmit={submit} style={{ display: "grid", gap: "1.1rem" }}>
        <div>
          <h1 style={{ margin: 0 }}>Practice with AI personas</h1>
          <p className="arena-hint" style={{ textAlign: "left", marginTop: "0.4rem" }}>
            You speak, they answer out loud. Cutting a persona off counts as an interruption and
            costs points, exactly as in a group discussion.
          </p>
        </div>

        {error && <div className="alert alert--error">{error}</div>}

        <div className="field">
          <label htmlFor="topic">Topic</label>
          <textarea
            id="topic"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="Should companies make remote work the default?"
            minLength={5}
            required
          />
        </div>

        <div>
          <label>
            Who is at the table? Pick 1 to {MAX_PERSONAS}.
          </label>
          <div className="picks" style={{ marginTop: "0.5rem" }}>
            {presets.map((p) => (
              <button
                key={p.key}
                type="button"
                className="pick"
                aria-pressed={chosen.includes(p.key)}
                onClick={() => toggle(p.key)}
              >
                <Orb seat={{ label: p.key, name: p.name, speaking: false, hue: hueFor(p.key) }} />
                <strong>{p.name}</strong>
                <small>
                  {p.tagline}. {p.brief}
                </small>
              </button>
            ))}
          </div>
        </div>

        <div className="grid">
          <div className="field">
            <label htmlFor="minutes">Length</label>
            <Select<number>
              id="minutes"
              value={minutes}
              onChange={setMinutes}
              options={[3, 5, 8, 10, 15].map((m) => ({ value: m, label: `${m} minutes` }))}
            />
          </div>
          <div className="field">
            <label htmlFor="difficulty">Difficulty</label>
            <Select<string>
              id="difficulty"
              value={difficulty}
              onChange={setDifficulty}
              options={[
                { value: "beginner", label: "Beginner" },
                { value: "intermediate", label: "Intermediate" },
                { value: "advanced", label: "Advanced" },
              ]}
            />
          </div>
        </div>

        <p className="arena-hint" style={{ textAlign: "left" }}>
          {voice === "elevenlabs"
            ? "Each persona speaks in their own voice."
            : "No voice service is configured, so your browser reads the personas aloud. Set TTS_PROVIDER=edge in .env for natural voices."}{" "}
          Use headphones so the personas&rsquo; voices do not trigger your microphone.
        </p>

        <div className="row">
          <button
            type="submit"
            className="arena-btn arena-btn--primary"
            disabled={busy || chosen.length === 0 || topic.trim().length < 5}
          >
            {busy ? "Setting up…" : "Enter the room"}
          </button>
        </div>
      </form>
    </div>
  );
}
