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

import { Icon } from "@/app/icons";
import { AnimatedBar, ScoreRing, Skeleton } from "@/app/motion";
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

  const pending = status != null && PENDING_STATES.has(status.state);
  // Before the first report lands the evaluation is either running or about to.
  const waiting =
    session != null &&
    !report &&
    (pending || (!status && ["ENDED", "EVALUATING"].includes(session.state)));

  // Evaluation is asynchronous by design, so the page polls until it lands —
  // including before the job exists: opened straight from "Finish", the first
  // status call can 404, and polling only on a pending status left the page
  // saying "updates by itself" while it never asked again (19 Oct, Day 4 walk).
  useEffect(() => {
    if (!pending && !waiting) return;
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [pending, waiting, load]);

  if (!session) return error ? <p className="empty">{error}</p> : <Skeleton rows={3} tall />;

  return (
    <div className="stack" style={{ gap: "1.25rem" }}>
      <header className="page-head page-head--back">
        <Link href={isHost ? "/" : "/history"} className="back" aria-label="Back">
          <Icon name="arrow-left" />
        </Link>
        <div style={{ minWidth: 0 }}>
          <h1>{waiting ? "Discussion complete" : isHost ? "Results" : "Your feedback"}</h1>
          <p>
            {session.topic}
            <span className="print-only"> · GD Platform · generated {new Date().toLocaleString()}</span>
          </p>
        </div>
      </header>

      {error && <div className="alert alert--error">{error}</div>}

      {status?.state === "FAILED" && (
        <div className="no-print">
          <EvaluationProgress status={status} />
        </div>
      )}

      {waiting && <Completing status={status} session={session} />}

      {isHost && scores && scores.length > 0 && (
        <div className="no-print">
          <ParticipantsTable
            scores={scores}
            sessionId={id}
            participants={session.participants}
            selected={candidate}
          />
        </div>
      )}

      <div id="candidate-report">
        {report ? (
          <CandidateReportCard
            body={report.body}
            session={session}
            status={status}
            transcript={transcript}
          />
        ) : waiting ? null : isHost ? (
          scores &&
          scores.length > 0 && (
            <div className="card">
              <p className="empty">Pick a participant above to open their full report.</p>
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

/** Shown while the evaluation runs: where it is, and what the session was. */
function Completing({ status, session }: { status: EvaluationStatus | null; session: GDSession }) {
  const state = status?.state ?? "QUEUED";
  const step = state === "REPORT_GENERATING" ? 2 : ["RUNNING", "WAITING_FOR_AGENT", "AGGREGATING"].includes(state) ? 1 : 0;
  const labels = ["Analysing discussion", "Evaluating participants", "Generating reports"];
  const humans = session.participants.filter((p) => !p.display_name.endsWith("(AI)")).length;
  const bots = session.participants.length - humans;
  return (
    <div className="complete-grid">
      <section className="card complete-card">
        <span className="complete-badge" aria-hidden>
          <Icon name="check" size={34} />
        </span>
        <h2>Discussion completed</h2>
        <p className="meta">
          We are analysing the discussion and generating detailed feedback for each participant.
        </p>
        <ol className="progress-steps">
          {labels.map((label, i) => (
            <li key={label} className={i < step ? "done" : i === step ? "now" : ""}>
              <span className="progress-dot">{i < step ? <Icon name="check" size={12} /> : null}</span>
              {label}
            </li>
          ))}
        </ol>
        <div className="progress-track" aria-hidden>
          <span style={{ width: `${[18, 55, 88][step]}%` }} />
        </div>
        <p className="meta">This may take a few minutes. This page updates by itself.</p>
      </section>

      <section className="card">
        <h2 className="section-title">Session details</h2>
        <ul className="detail-list">
          <li>
            <Icon name="chat" size={18} />
            <span>
              <small>Topic</small>
              {session.topic}
            </span>
          </li>
          <li>
            <Icon name="clock" size={18} />
            <span>
              <small>Duration</small>
              {Math.round(session.duration_seconds / 60)} minutes
            </span>
          </li>
          <li>
            <Icon name="users" size={18} />
            <span>
              <small>Participants</small>
              {humans > 0 && `${humans} ${humans === 1 ? "person" : "people"}`}
              {humans > 0 && bots > 0 && " and "}
              {bots > 0 && `${bots} AI`}
            </span>
          </li>
          <li>
            <Icon name="calendar" size={18} />
            <span>
              <small>Date</small>
              {new Date(session.created_at ?? Date.now()).toLocaleString()}
            </span>
          </li>
        </ul>
      </section>
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

function ratingWord(score: number): string {
  if (score >= 80) return "Excellent";
  if (score >= 65) return "Good";
  if (score >= 50) return "Fair";
  return "Needs work";
}

function downloadPdf(name: string) {
  // The browser's own "Save as PDF" — the print stylesheet in globals.css lays
  // the report out for paper. The title becomes the suggested file name.
  const previous = document.title;
  document.title = `GD report - ${name}`;
  window.print();
  document.title = previous;
}

type ReportTab = "summary" | "analysis" | "timeline" | "suggestions";

const REPORT_TABS: [ReportTab, string][] = [
  ["summary", "Summary"],
  ["analysis", "Detailed analysis"],
  ["timeline", "Speaking timeline"],
  ["suggestions", "Suggestions"],
];

function CandidateReportCard({
  body,
  session,
  status,
  transcript,
}: {
  body: CandidateReportBody;
  session: GDSession;
  status: EvaluationStatus | null;
  transcript: TranscriptOut | null;
}) {
  const [tab, setTab] = useState<ReportTab>("summary");
  const isAI = body.display_name.endsWith("(AI)");
  const name = body.display_name.replace(/\s*\(AI\)$/, "");
  const synthesis = readSynthesis(body.synthesis);
  return (
    <div className="card report-card">
      <div className="report-head">
        <div style={{ flex: 1, minWidth: "14rem" }}>
          <h2 style={{ fontSize: "1.4rem", margin: "0 0 0.2rem" }}>
            {name} {isAI && <span className="badge">AI</span>}
          </h2>
          <p className="meta" style={{ margin: 0 }}>
            Candidate {body.candidate_label} · {session.gd_type.replace("_", " ")} GD ·{" "}
            {Math.round(session.duration_seconds / 60)} min
          </p>
        </div>
        <button type="button" className="secondary no-print" onClick={() => downloadPdf(name)}>
          <Icon name="download" size={16} /> Download PDF
        </button>
      </div>

      <div className="tabs tabs--page no-print" role="tablist">
        {REPORT_TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`tab ${tab === key ? "tab--on" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {body.needs_human_review && (
        <div className="alert alert--error" style={{ marginTop: "1rem" }}>
          Flagged for human review: {body.review_reasons.join("; ")}.
        </div>
      )}

      <div key={tab} className="tab-panel">
        {tab === "summary" && <SummaryTab body={body} synthesis={synthesis} />}
        {tab === "analysis" && (
          <AnalysisTab body={body} session={session} synthesis={synthesis} status={status} />
        )}
        {tab === "timeline" && (
          <TimelineTab body={body} session={session} transcript={transcript} />
        )}
        {tab === "suggestions" && <SuggestionsTab body={body} synthesis={synthesis} />}
      </div>
    </div>
  );
}

function SummaryTab({
  body,
  synthesis,
}: {
  body: CandidateReportBody;
  synthesis: Partial<SynthesisBody> | null;
}) {
  return (
    <>
      <div className="report-top">
        <div className="report-ring">
          <ScoreRing score={body.overall_score} toneClass={scoreClass(body.overall_score)} />
          <strong className={`rating ${scoreClass(body.overall_score)}`}>
            {ratingWord(body.overall_score)}
          </strong>
          <span className="meta">Overall score</span>
        </div>

        <div className="report-bars">
          <h3 className="report-section" style={{ marginTop: 0 }}>
            Performance across skills
          </h3>
          {body.dimensions.map((d) => (
            <div key={d.dimension} className="score-row">
              <span>{DIMENSION_LABELS[d.dimension] ?? d.dimension}</span>
              <AnimatedBar value={d.score_100} tone={scoreTone(d.score_100)} />
              <strong>{Math.round(d.score_100)}</strong>
            </div>
          ))}
        </div>
      </div>

      <div className="insight-grid">
        <div className="insight insight--good">
          <h3>
            <Icon name="check-circle" size={18} /> Strengths
          </h3>
          {body.strengths.length > 0 ? (
            <ul>
              {body.strengths.slice(0, 5).map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ) : (
            <p className="meta">No standout strengths were recorded.</p>
          )}
        </div>
        <div className="insight insight--bad">
          <h3>
            <Icon name="target" size={18} /> Areas to improve
          </h3>
          {body.weaknesses.length > 0 ? (
            <ul>
              {body.weaknesses.slice(0, 5).map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ) : (
            <p className="meta">No weaknesses were recorded.</p>
          )}
        </div>
      </div>

      {synthesis?.summary && (
        <section className="narrative">
          <h3 className="report-section">Detailed feedback</h3>
          <p>{synthesis.summary}</p>
          {synthesis.pattern && <p className="meta">{synthesis.pattern}</p>}
          {(synthesis.strengths?.length ?? 0) > 0 && (
            <>
              <h4>What to keep doing</h4>
              <ul className="keep-list">
                {synthesis.strengths!.map((item, i) => (
                  <li key={i}>
                    <strong>{item.point}.</strong> {item.why_it_helped}
                    <div className="meta">{item.evidence}</div>
                  </li>
                ))}
              </ul>
            </>
          )}
          {synthesis.caveat && (
            <p className="meta" style={{ fontStyle: "italic" }}>
              Note for review: {synthesis.caveat}
            </p>
          )}
        </section>
      )}
    </>
  );
}

function SuggestionsTab({
  body,
  synthesis,
}: {
  body: CandidateReportBody;
  synthesis: Partial<SynthesisBody> | null;
}) {
  const areas = synthesis?.focus_areas ?? [];
  if (areas.length === 0) {
    return (
      <section className="narrative">
        {synthesis?.priority_improvement ? (
          <>
            <h3 className="report-section">Work on this first</h3>
            <p>{synthesis.priority_improvement}</p>
          </>
        ) : body.weaknesses.length > 0 ? (
          <>
            <h3 className="report-section">What to work on</h3>
            <ul className="keep-list">
              {body.weaknesses.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </>
        ) : (
          <p className="empty">No suggestions were recorded for this report.</p>
        )}
      </section>
    );
  }
  return (
    <section className="suggestions">
      {areas.map((item, i) => (
        <article key={i} className="suggestion">
          <header>
            <span className="step-no">{i + 1}</span>
            <h3>{item.area}</h3>
            {i === 0 && <span className="chip chip--info">Start here</span>}
          </header>
          <dl>
            <dt>What happened</dt>
            <dd>{item.what_happened}</dd>
            {item.why_it_matters && (
              <>
                <dt>Why it matters</dt>
                <dd>{item.why_it_matters}</dd>
              </>
            )}
            <dt>What to do</dt>
            <dd>{item.what_to_do}</dd>
          </dl>
          {item.try_saying && (
            <p className="try-saying">
              <Icon name="chat" size={16} />
              <span>
                <small>Try saying</small>
                {item.try_saying}
              </span>
            </p>
          )}
          {item.practice && (
            <p className="try-saying try-saying--practice">
              <Icon name="lightbulb" size={16} />
              <span>
                <small>Practice</small>
                {item.practice}
              </span>
            </p>
          )}
        </article>
      ))}
    </section>
  );
}

function AnalysisTab({
  body,
  session,
  synthesis,
  status,
}: {
  body: CandidateReportBody;
  session: GDSession;
  synthesis: Partial<SynthesisBody> | null;
  status: EvaluationStatus | null;
}) {
  return (
    <>
      {(synthesis?.dimension_notes?.length ?? 0) > 0 && (
        <section className="narrative">
          <h3 className="report-section">What each score means</h3>
          <dl className="score-notes">
            {synthesis!.dimension_notes!.map((item, i) => (
              <div key={i}>
                <dt>{item.dimension}</dt>
                <dd className="meta">{item.what_it_measures}</dd>
                <dd>{item.how_you_did}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      <ScoreBreakdown body={body} session={session} />

      <h3 className="report-section">Why each score: the evaluators&rsquo; reasoning</h3>
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

      {(synthesis?.tool_calls?.length ?? 0) > 0 && (
        <details className="meta" style={{ marginTop: "1rem" }}>
          <summary style={{ cursor: "pointer" }}>
            How the written feedback was checked ({synthesis!.tool_calls!.length} scoring{" "}
            {synthesis!.tool_calls!.length === 1 ? "lookup" : "lookups"})
          </summary>
          <p style={{ margin: "0.4rem 0 0" }}>
            The narrative was written by a model that could <em>read</em> the score through a tool
            but could not change it. Every number it quotes came from the deterministic
            aggregator.
          </p>
        </details>
      )}

      {status && (
        <p className="footnote">
          Evaluation {status.state.replace(/_/g, " ").toLowerCase()}. Rubric v{status.rubric_version}
          , prompts v{status.prompt_version}, agents v{status.agent_version}. These are recorded so
          any score can be traced back to the configuration that produced it.
        </p>
      )}
    </>
  );
}

function TimelineTab({
  body,
  session,
  transcript,
}: {
  body: CandidateReportBody;
  session: GDSession;
  transcript: TranscriptOut | null;
}) {
  const total = Math.max(1, session.duration_seconds);
  const toSeconds = (stamp: string) => {
    const [m, sec] = stamp.split(":").map(Number);
    return (m || 0) * 60 + (sec || 0);
  };
  const mine = (transcript?.lines ?? []).filter((l) => l.candidate_label === body.candidate_label);
  const cuts = body.interruption_times ?? [];
  return (
    <>
      <MetricsGrid metrics={body.metrics} />
      <h3 className="report-section">When {body.display_name.replace(/\s*\(AI\)$/, "")} spoke</h3>
      <div className="timeline" role="img" aria-label="Speaking timeline">
        <div className="timeline-track" />
        {mine.map((line, i) => (
          <span
            key={i}
            className="timeline-dot"
            style={{ left: `${Math.min(99, (toSeconds(line.timestamp) / total) * 100)}%` }}
            title={`${line.timestamp} ${line.text}`}
          />
        ))}
        {cuts.map((t, i) => (
          <span
            key={`c${i}`}
            className="timeline-dot timeline-dot--cut"
            style={{ left: `${Math.min(99, (t / total) * 100)}%` }}
            title={`Interruption at ${formatTime(t)}`}
          />
        ))}
        <div className="timeline-scale">
          <span>00:00</span>
          <span>{formatTime(total / 2)}</span>
          <span>{formatTime(total)}</span>
        </div>
      </div>
      <p className="meta">
        Blue marks are contributions; red marks are interruptions.
      </p>
      {mine.length === 0 ? (
        <p className="empty">No transcript lines were captured for this candidate.</p>
      ) : (
        <ul className="feed feed--plain">
          {mine.map((line, i) => (
            <li key={i}>
              <time>{line.timestamp}</time>
              <p>{line.text}</p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function readSynthesis(raw: CandidateReportBody["synthesis"]): Partial<SynthesisBody> | null {
  // Optional three ways: the synthesis agent may have failed, and reports stored
  // before it existed have no `synthesis` key at all.
  if (!raw || typeof raw !== "object") return null;
  const s = raw as Partial<SynthesisBody>;
  return s.summary || s.focus_areas?.length || s.priority_improvement ? s : null;
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

function ParticipantsTable({
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
  const dims = Array.from(new Set(ranked.flatMap((s) => Object.keys(s.dimensions))));
  return (
    <div className="card">
      <div className="row row--between" style={{ marginBottom: "0.8rem" }}>
        <h2 className="section-title" style={{ margin: 0 }}>
          All participants
        </h2>
        <span className="meta">Select a participant to open their report</span>
      </div>
      <div className="table-wrap">
        <table className="results-table">
          <thead>
            <tr>
              <th>Participant</th>
              <th>Overall</th>
              {dims.map((d) => (
                <th key={d}>{DIMENSION_LABELS[d] ?? d}</th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {ranked.map((score, index) => {
              const display = names.get(score.candidate_label) ?? `Candidate ${score.candidate_label}`;
              const isAI = display.endsWith("(AI)");
              return (
                <tr
                  key={score.candidate_label}
                  className={selected === score.candidate_label ? "row--selected" : ""}
                >
                  <td>
                    <Link
                      href={`/report/${sessionId}?candidate=${score.candidate_label}#candidate-report`}
                      scroll={false}
                      className="who"
                    >
                      <span className="avatar">{score.candidate_label}</span>
                      <span>
                        {display.replace(/\s*\(AI\)$/, "")}
                        {isAI && <span className="badge">AI</span>}
                        {index === 0 && (
                          <span className="top" title="Highest overall score">
                            <Icon name="trophy" size={14} />
                          </span>
                        )}
                        {score.needs_human_review && <small className="meta"> needs review</small>}
                      </span>
                    </Link>
                  </td>
                  <td>
                    <span className={`score-pill ${scoreClass(score.overall)}`}>
                      {Math.round(score.overall)}
                    </span>
                  </td>
                  {dims.map((d) => (
                    <td key={d}>
                      {score.dimensions[d] != null ? Math.round(score.dimensions[d] * 10) : "–"}
                    </td>
                  ))}
                  <td>
                    <Link
                      href={`/report/${sessionId}?candidate=${score.candidate_label}#candidate-report`}
                      scroll={false}
                      className="view-link"
                    >
                      View <Icon name="arrow-right" size={14} />
                    </Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
