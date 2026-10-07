"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Orb, hueFor } from "@/app/immersive/parts";
import { ApiError, practice } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { PersonaPreset } from "@/lib/types";

const MAX_PERSONAS = 4;

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

  function toggle(key: string) {
    setChosen((prev) =>
      prev.includes(key)
        ? prev.filter((k) => k !== key)
        : prev.length < MAX_PERSONAS
          ? [...prev, key]
          : prev,
    );
  }

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
            <select id="minutes" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
              {[3, 5, 8, 10, 15].map((m) => (
                <option key={m} value={m}>
                  {m} minutes
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="difficulty">Difficulty</label>
            <select id="difficulty" value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
              <option value="beginner">Beginner</option>
              <option value="intermediate">Intermediate</option>
              <option value="advanced">Advanced</option>
            </select>
          </div>
        </div>

        <p className="arena-hint" style={{ textAlign: "left" }}>
          {voice === "elevenlabs"
            ? "Personas speak with ElevenLabs voices."
            : "No ElevenLabs key is set, so your browser reads the personas aloud. Add ELEVENLABS_API_KEY to .env for natural voices."}{" "}
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
