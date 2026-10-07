"use client";

/**
 * The report (FR-29 – FR-31, design §33, §34).
 *
 * Every score shown here is displayed with the evidence behind it. That is not
 * decoration: NFR-08 and claude.md invariant 6 require that no number appears
 * without a transcript timestamp or a measured quantity supporting it, so the
 * UI has no way to render a bare score.
 */

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, evaluation, sessions } from "@/lib/api";
import { TranscriptCard } from "@/app/transcript-card";
import { useAuth } from "@/lib/auth-context";
import type {
  CandidateReportBody,
  SynthesisBody,
  EvaluationStatus,
  GDSession,
  GroupReportBody,
  ReportOut,
  ScoreOut,
  TranscriptOut,
} from "@/lib/types";

const PENDING_STATES = new Set([
  "QUEUED",
  "PREPARING",
  "RUNNING",
  "WAITING_FOR_AGENT",
  "AGGREGATING",
  "REPORT_GENERATING",
]);

const DIMENSION_LABELS: Record<string, string> = {
  content: "Content",
  communication: "Communication",
  leadership: "Leadership",
  collaboration: "Collaboration",
  critical_thinking: "Critical thinking",
  behavior: "Behaviour",
};

export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  // The host's score table links here with ?candidate=B to open that report.
  const candidate = useSearchParams().get("candidate");
  const { user } = useAuth();

  const [session, setSession] = useState<GDSession | null>(null);
  const [status, setStatus] = useState<EvaluationStatus | null>(null);
  const [report, setReport] = useState<ReportOut<CandidateReportBody> | null>(null);
  const [group, setGroup] = useState<ReportOut<GroupReportBody> | null>(null);
  const [scores, setScores] = useState<ScoreOut[] | null>(null);
  const [transcript, setTranscript] = useState<TranscriptOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isHost = session != null && user != null && session.host_id === user.id;

  const load = useCallback(async () => {
    try {
      const loaded = await sessions.get(id);
      setSession(loaded);

      // Any of these may legitimately 404 while evaluation is still running.
      await Promise.allSettled([
        evaluation.status(id).then(setStatus),
        (candidate ? evaluation.candidateReport(id, candidate) : evaluation.myReport(id)).then(
          setReport,
        ),
        sessions.transcript(id).then(setTranscript),
      ]);
      if (user && loaded.host_id === user.id) {
        await Promise.allSettled([
          evaluation.groupReport(id).then(setGroup),
          evaluation.scores(id).then(setScores),
        ]);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not load the report");
    }
  }, [id, user, candidate]);

  useEffect(() => {
    void load();
  }, [load]);

  // Evaluation is asynchronous by design, so the page polls until it lands.
  useEffect(() => {
    if (!status || !PENDING_STATES.has(status.state)) return;
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [status, load]);

  if (!session) return <p className="empty">{error ?? "Loading…"}</p>;

  const pending = status != null && PENDING_STATES.has(status.state);

  return (
    <div className="stack">
      <div className="card">
        <h1>{session.topic}</h1>
        <p className="subtitle" style={{ marginBottom: 0 }}>
          {session.gd_type.replace("_", " ")} · {session.difficulty} · {session.participants.length}{" "}
          participants · {Math.round(session.duration_seconds / 60)} minutes
        </p>
        <p className="print-only meta" style={{ margin: "0.3rem 0 0" }}>
          GD Platform · candidate evaluation report · generated {new Date().toLocaleString()}
        </p>
      </div>

      {error && <div className="alert alert--error">{error}</div>}

      {status && (
        <div className="no-print">
          <EvaluationProgress status={status} />
        </div>
      )}

      {isHost && scores && scores.length > 0 && (
        <div className="no-print">
        <Leaderboard
          scores={scores}
          sessionId={id}
          participants={session.participants}
          selected={candidate}
        />
        </div>
      )}

      <div id="candidate-report">
        {report ? (
          <CandidateReportCard body={report.body} session={session} />
        ) : pending ? (
          <div className="card">
            <p className="empty">
              Reports are being generated. This runs after the discussion, so nothing here depended
              on it while anyone was talking.
            </p>
          </div>
        ) : isHost ? (
          scores &&
          scores.length > 0 && (
            <div className="card">
              <p className="empty">Pick a candidate above to open their full report.</p>
            </div>
          )
        ) : (
          <div className="card">
            <p className="empty">No report for you in this session yet.</p>
          </div>
        )}
      </div>

      <div className="no-print stack">
        {isHost && group && <GroupReportCard body={group.body} />}
        {transcript && <TranscriptCard transcript={transcript} />}
      </div>
    </div>
  );
}

function EvaluationProgress({ status }: { status: EvaluationStatus }) {
  const done = status.state === "COMPLETED";
  const failed = status.state === "FAILED";
  return (
    <div className="card">
      <div className="row row--between row--wrap">
        <div>
          <h2 style={{ fontSize: "1.05rem", margin: 0 }}>Evaluation</h2>
          <p className="meta" style={{ margin: "0.2rem 0 0" }}>
            {status.selected_agents.map((a) => DIMENSION_LABELS[a] ?? a).join(" · ")}
          </p>
        </div>
        <span className={`pill ${done ? "pill--live" : failed ? "pill--warn" : ""}`}>
          {status.state.replace(/_/g, " ").toLowerCase()}
        </span>
      </div>
      {failed && status.error && (
        <div className="alert alert--error" style={{ marginTop: "0.8rem" }}>
          {status.error}
        </div>
      )}
      <p className="footnote">
        Rubric v{status.rubric_version} · prompts v{status.prompt_version} · agents v
        {status.agent_version}. These are recorded so any score can be traced back to the exact
        configuration that produced it.
      </p>
    </div>
  );
}

function downloadPdf(name: string) {
  // The browser's own "Save as PDF" — the print stylesheet in globals.css lays
  // the report out for paper. The title becomes the suggested file name.
  const previous = document.title;
  document.title = `GD report - ${name}`;
  window.print();
  document.title = previous;
}

function CandidateReportCard({
  body,
  session,
}: {
  body: CandidateReportBody;
  session: GDSession;
}) {
  const isAI = body.display_name.endsWith("(AI)");
  const name = body.display_name.replace(/\s*\(AI\)$/, "");
  return (
    <div className="card report-card">
      <div className="report-head">
        <div
          className={`score-ring ${scoreClass(body.overall_score)}`}
          style={{ "--p": Math.min(100, body.overall_score) } as React.CSSProperties}
          aria-label={`Overall ${Math.round(body.overall_score)} out of 100`}
        >
          <span>
            <strong>{Math.round(body.overall_score)}</strong>
            <small>/ 100</small>
          </span>
        </div>
        <div style={{ flex: 1, minWidth: "14rem" }}>
          <span className="eyebrow">Candidate {body.candidate_label}</span>
          <h2 style={{ fontSize: "1.5rem", margin: "0.5rem 0 0.2rem" }}>
            {name} {isAI && <span className="badge">AI</span>}
          </h2>
          <p className="meta" style={{ margin: 0 }}>
            {session.topic} · {session.gd_type.replace("_", " ")} GD ·{" "}
            {Math.round(session.duration_seconds / 60)} min
          </p>
        </div>
        <button type="button" className="secondary no-print" onClick={() => downloadPdf(name)}>
          ⬇ Download PDF
        </button>
      </div>

      <SynthesisPanel synthesis={body.synthesis} />

      {body.needs_human_review && (
        <div className="alert alert--error" style={{ marginTop: "1rem" }}>
          Flagged for human review: {body.review_reasons.join("; ")}.
        </div>
      )}

      <ScoreBreakdown body={body} session={session} />

      <MetricsGrid metrics={body.metrics} />

      <h3 className="report-section">Why each score — the evaluators&rsquo; reasoning</h3>
      <div className="dimension-grid">
        {body.dimensions.map((dimension) => (
          <div key={dimension.dimension} className="dimension-card">
            <div className="row row--between">
              <strong>{DIMENSION_LABELS[dimension.dimension] ?? dimension.dimension}</strong>
              <span className={`dimension-score ${scoreClass(dimension.score_100)}`}>
                {dimension.score_10.toFixed(1)}
                <small>/10</small>
              </span>
            </div>
            <div
              className={`bar bar--${scoreTone(dimension.score_100)}`}
              style={{ margin: "0.5rem 0 0.4rem" }}
            >
              <span style={{ width: `${Math.min(100, dimension.score_100)}%` }} />
            </div>
            <p className="meta" style={{ margin: "0 0 0.6rem" }}>
              weight {(dimension.weight * 100).toFixed(1)}% · evaluator confidence{" "}
              {(dimension.confidence * 100).toFixed(0)}%
            </p>
            {dimension.strengths.length > 0 && (
              <ul className="reasons reasons--good">
                {dimension.strengths.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            )}
            {dimension.weaknesses.length > 0 && (
              <ul className="reasons reasons--bad">
                {dimension.weaknesses.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            )}
            {dimension.evidence.length > 0 && (
              <ul className="evidence">
                {dimension.evidence.map((item, index) => (
                  <li key={index}>
                    <code>{item.timestamp}</code> {item.summary}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * The final score is arithmetic, not judgement (claude.md invariant 5), so it
 * can be shown as the exact equation that produced it.
 */
function ScoreBreakdown({ body, session }: { body: CandidateReportBody; session: GDSession }) {
  const rows = body.dimensions.filter((d) => d.weight > 0);
  const total = rows.reduce((sum, d) => sum + d.score_10 * d.weight * 10, 0);
  const rubric = session.rubric_weights ?? {};
  const notEvaluated = Object.keys(rubric).filter(
    (dimension) => !rows.some((d) => d.dimension === dimension),
  );
  const foldsCriticalThinking = session.gd_type === "technical" || session.gd_type === "hr_social";
  const label = (dimension: string) => DIMENSION_LABELS[dimension] ?? dimension;

  return (
    <>
      <h3 className="report-section">How this score was calculated</h3>
      <div className="formula">
        Overall = 10 × Σ (dimension score × weight)
        <br />= 10 × (
        {rows.map((d, index) => (
          <span key={d.dimension}>
            {index > 0 && " + "}
            {d.score_10.toFixed(1)}×{d.weight.toFixed(3)}
          </span>
        ))}
        ) = <strong>{total.toFixed(1)}</strong>
        {(body.penalty ?? 0) > 0 && (
          <>
            <br />
            Interruption penalty = −<strong>{body.penalty}</strong> (
            {body.interruption_times?.length ?? 0} interruption
            {(body.interruption_times?.length ?? 0) === 1 ? "" : "s"} at{" "}
            {(body.interruption_times ?? []).map((t) => formatTime(t)).join(", ")})
            <br />
            Final = <strong>{body.overall_score.toFixed(1)}</strong>
          </>
        )}
      </div>
      <table className="breakdown">
        <thead>
          <tr>
            <th>Dimension</th>
            <th>Rubric weight</th>
            <th>Applied weight</th>
            <th>Score /10</th>
            <th style={{ textAlign: "right" }}>Points</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.dimension}>
              <td>{label(d.dimension)}</td>
              <td>{rubric[d.dimension] != null ? `${(rubric[d.dimension] * 100).toFixed(0)}%` : "—"}</td>
              <td>{(d.weight * 100).toFixed(1)}%</td>
              <td>{d.score_10.toFixed(1)}</td>
              <td style={{ textAlign: "right" }}>{(d.score_10 * d.weight * 10).toFixed(1)}</td>
            </tr>
          ))}
          {notEvaluated.map((dimension) => (
            <tr key={dimension} className="meta">
              <td>{label(dimension)}</td>
              <td>{(rubric[dimension] * 100).toFixed(0)}%</td>
              <td>0% — not evaluated</td>
              <td>—</td>
              <td style={{ textAlign: "right" }}>—</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4}>
              <strong>Overall</strong>
            </td>
            <td style={{ textAlign: "right" }}>
              <strong className={scoreClass(body.overall_score)}>
                {body.overall_score.toFixed(1)} / 100
              </strong>
            </td>
          </tr>
        </tfoot>
      </table>
      <ul className="evidence" style={{ marginTop: "0.6rem" }}>
        <li>
          Each dimension score (0–10) is one evaluator agent&rsquo;s judgement, backed by the
          transcript evidence below. Combining them is plain arithmetic — no AI decides the final
          number, so the same scores always give the same result.
        </li>
        {notEvaluated.length > 0 && (
          <li>
            {notEvaluated.map(label).join(", ")} {notEvaluated.length === 1 ? "is" : "are"} not
            evaluated for a {session.gd_type.replace("_", " ")} GD (or the evaluator failed), so{" "}
            {notEvaluated.length === 1 ? "its" : "their"} weight was shared out across the other
            dimensions in proportion — a missing evaluation never counts as zero.
          </li>
        )}
        {foldsCriticalThinking && (
          <li>
            Critical thinking has no weight of its own: it is scored under Content (averaged with it
            when both evaluators ran).
          </li>
        )}
      </ul>
    </>
  );
}

function formatTime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function SynthesisPanel({ synthesis }: { synthesis: CandidateReportBody["synthesis"] }) {
  // The narrative is optional three ways: the synthesis agent may have failed,
  // and reports stored before this field existed have no `synthesis` key at
  // all. Check for the object before reaching into it — `in` throws on
  // undefined, and a report from last week is exactly the case that hits it.
  if (!synthesis || typeof synthesis !== "object") return null;
  const s = synthesis as Partial<SynthesisBody>;
  if (!s.summary) return null;

  return (
    <div
      style={{
        borderLeft: "3px solid var(--accent)",
        paddingLeft: "1rem",
        margin: "1.25rem 0 0",
      }}
    >
      <p style={{ margin: "0 0 0.6rem", fontSize: "1rem" }}>{s.summary}</p>
      {s.pattern && (
        <p className="meta" style={{ margin: "0 0 0.6rem" }}>
          {s.pattern}
        </p>
      )}
      {s.priority_improvement && (
        <p style={{ margin: "0 0 0.4rem", fontSize: "0.92rem" }}>
          <strong>Work on this first:</strong> {s.priority_improvement}
        </p>
      )}
      {s.caveat && (
        <p className="meta" style={{ margin: "0 0 0.4rem", fontStyle: "italic" }}>
          Note for review: {s.caveat}
        </p>
      )}
      {(s.tool_calls?.length ?? 0) > 0 && (
        <details className="meta" style={{ marginTop: "0.5rem" }}>
          <summary style={{ cursor: "pointer" }}>
            How this was checked ({s.tool_calls!.length} scoring{" "}
            {s.tool_calls!.length === 1 ? "lookup" : "lookups"})
          </summary>
          <p style={{ margin: "0.4rem 0 0" }}>
            The narrative above was written by a model that could <em>read</em> the score through a
            tool but could not change it. Every number it quotes came from the deterministic
            aggregator.
          </p>
          <ul className="evidence">
            {s.tool_calls!.map((call, index) => (
              <li key={index}>
                <code>{call.tool}</code>
                {Object.keys(call.arguments).length > 0 &&
                  ` (${Object.entries(call.arguments)
                    .map(([k, v]) => `${k}=${String(v)}`)
                    .join(", ")})`}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function MetricsGrid({ metrics }: { metrics: CandidateReportBody["metrics"] }) {
  const items: [string, string][] = [
    ["Speaking time", `${Math.round(metrics.speaking_seconds)}s`],
    ["Share of floor", `${(metrics.participation_ratio * 100).toFixed(0)}%`],
    ["Turns", String(metrics.turns)],
    ["Interruptions", String(metrics.interruptions)],
    ["Direct responses", String(metrics.responses)],
    ["Questions asked", String(metrics.questions)],
  ];
  return (
    <>
      <h3 style={{ fontSize: "0.95rem", margin: "1.5rem 0 0.6rem" }}>
        Measured behaviour{" "}
        <span className="meta" style={{ fontWeight: 400 }}>
          — computed from speech events, not estimated
        </span>
      </h3>
      <div className="grid">
        {items.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd style={{ fontSize: "1.15rem" }}>{value}</dd>
          </div>
        ))}
      </div>
    </>
  );
}

function GroupReportCard({ body }: { body: GroupReportBody }) {
  return (
    <div className="card">
      <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.9rem" }}>Group summary</h2>
      <div className="grid">
        <div>
          <dt>Discussion quality</dt>
          <dd>{body.discussion_quality}</dd>
        </div>
        <div>
          <dt>Consensus</dt>
          <dd>{body.consensus}</dd>
        </div>
        <div>
          <dt>Dominant</dt>
          <dd>{body.dominant_participants.join(", ") || "nobody dominated"}</dd>
        </div>
        <div>
          <dt>Under-participating</dt>
          <dd>{body.under_participating.join(", ") || "everyone contributed"}</dd>
        </div>
      </div>

      {body.main_themes.length > 0 && (
        <>
          <dt>Main themes</dt>
          <dd>
            <ol style={{ margin: "0.2rem 0 0.9rem", paddingLeft: "1.2rem" }}>
              {body.main_themes.map((theme, index) => (
                <li key={index}>{theme}</li>
              ))}
            </ol>
          </dd>
        </>
      )}

      <dt>Turning point</dt>
      <dd>{body.turning_point}</dd>

      {body.unresolved_questions.length > 0 && (
        <>
          <dt>Left unresolved</dt>
          <dd>{body.unresolved_questions.join("; ")}</dd>
        </>
      )}

      {body.agent_conflicts && body.agent_conflicts.length > 0 && (
        <div className="alert alert--error">
          Evaluator disagreement worth reviewing: {body.agent_conflicts.join("; ")}
        </div>
      )}
      {body.agent_failures && body.agent_failures.length > 0 && (
        <p className="footnote">
          Some evaluators did not return a result: {body.agent_failures.join("; ")}. Their weight
          was redistributed across the dimensions that did score.
        </p>
      )}
    </div>
  );
}

function scoreTone(score100: number): "good" | "mid" | "low" {
  return score100 >= 70 ? "good" : score100 >= 50 ? "mid" : "low";
}

function scoreClass(score100: number): string {
  return `score--${scoreTone(score100)}`;
}

function Leaderboard({
  scores,
  sessionId,
  participants,
  selected,
}: {
  scores: ScoreOut[];
  sessionId: string;
  participants: GDSession["participants"];
  selected: string | null;
}) {
  const names = new Map(participants.map((p) => [p.candidate_label, p.display_name]));
  const ranked = [...scores].sort((a, b) => b.overall - a.overall);
  return (
    <div className="card">
      <div className="row row--between" style={{ marginBottom: "0.6rem" }}>
        <h2 className="card-title">Results</h2>
        <span className="meta">Click a candidate for their full report</span>
      </div>
      {ranked.map((score, index) => {
        const display = names.get(score.candidate_label) ?? `Candidate ${score.candidate_label}`;
        const isAI = display.endsWith("(AI)");
        return (
          <Link
            key={score.candidate_label}
            href={`/report/${sessionId}?candidate=${score.candidate_label}#candidate-report`}
            className={`leader ${selected === score.candidate_label ? "leader--selected" : ""}`}
            scroll={false}
          >
            <span className="rank">{index === 0 ? "🏆" : `#${index + 1}`}</span>
            <div className="row" style={{ minWidth: 0 }}>
              <span className="avatar">{score.candidate_label}</span>
              <div style={{ minWidth: 0 }}>
                <div className="tile-name">
                  {display.replace(/\s*\(AI\)$/, "")} {isAI && <span className="badge">AI</span>}
                </div>
                <div className="meta">
                  {(score.confidence * 100).toFixed(0)}% confidence
                  {score.needs_human_review && " · review"}
                </div>
              </div>
            </div>
            <div className="leader-bars">
              <div className={`bar bar--lg bar--${scoreTone(score.overall)}`}>
                <span style={{ width: `${Math.min(100, score.overall)}%` }} />
              </div>
              <div className="mini-dims">
                {Object.entries(score.dimensions).map(([dimension, value]) => (
                  <div
                    key={dimension}
                    title={`${DIMENSION_LABELS[dimension] ?? dimension}: ${value}/10`}
                  >
                    <div className="meta">{DIMENSION_LABELS[dimension] ?? dimension}</div>
                    <div className={`bar bar--${scoreTone(value * 10)}`}>
                      <span style={{ width: `${Math.min(100, value * 10)}%` }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
            <span
              className={`${scoreClass(score.overall)}`}
              style={{
                fontSize: "1.5rem",
                fontWeight: 650,
                textAlign: "right",
              }}
            >
              {Math.round(score.overall)}
            </span>
            <span className="leader-link meta">Details →</span>
          </Link>
        );
      })}
    </div>
  );
}
