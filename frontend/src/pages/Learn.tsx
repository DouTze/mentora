import { useEffect, useRef, useState } from "react";
import { api, RequestError } from "../api";
import type { ApiError, SessionView, TutorMessage } from "../types";
import { TutorBubble } from "../components/TutorBubble";
import { TurnList } from "../components/TurnList";
import { Progress } from "../components/Progress";
import { BackendBadge } from "../components/BackendBadge";
import { ReadingPanel } from "../components/ReadingPanel";
import { RichText } from "../components/RichText";
import { Working } from "../components/Working";
import { useT } from "../i18n";
import { usePrefs } from "../prefs";
import { CommandRefused, isGraph, useCommands } from "../workflow";
import { prepared } from "../readerMode";

const PREPARE_POLL_MS = 1500;

interface Props {
  sessionId: string | null;
  onSessionChange: (id: string) => void;
  onOpenMaterial?: (id: string) => void;
  /** opened by 出題 in the reader: go on to the questions as soon as the first guide is shown */
  autoQuiz?: boolean;
  onAutoQuizDone?: () => void;
  /** shown in the book's reader in place of its pages (閱讀模式): without the head naming the book and range, and
   *  `view` "guide" keeps to the guides (every one so far, one under another) while "quiz" goes on to the questions */
  embedded?: boolean;
  view?: "guide" | "quiz";
  /** the session as it changes (the reader lists its quotes in the table of contents) */
  onSession?: (s: SessionView) => void;
  /** the session could not be read */
  onError?: () => void;
}

export function LearnPage({ sessionId, onSessionChange, onOpenMaterial, autoQuiz, onAutoQuizDone, embedded = false, view, onSession, onError }: Props) {
  const t = useT();
  const { guideLang } = usePrefs();
  const [session, setSession] = useState<SessionView | null>(null);
  const [answer, setAnswer] = useState("");
  const [last, setLast] = useState<TutorMessage | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [restarting, setRestarting] = useState(false);
  // LangGraph sessions answer through a command; a failed one can be retried with its own id
  const quiz = !!session && session.state.phase !== "reading";
  const cmds = useCommands(session, setSession, quiz ? ["answer", "retry"] : ["answer"]);
  const [failedAnswer, setFailedAnswer] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    setLast(null);
    setError(null);
    api
      .session(sessionId)
      .then((s) => {
        setSession(s);
        setLast(lastMessage(s));
      })
      .catch((e: RequestError) => {
        setError(e.body);
        onError?.();
      });
  }, [sessionId]);
  // in the reader, a mode being prepared is read again until it has something to show: the reader fills the button
  // by how far it has come, and a workflow that has not reported a job yet is not polled by the reading panel
  const unprepared = embedded && !!view && !!session && !prepared(view, session);
  useEffect(() => {
    if (!unprepared || !sessionId) return;
    const timer = setInterval(() => {
      api
        .session(sessionId)
        .then(setSession)
        .catch(() => {});
    }, PREPARE_POLL_MS);
    return () => clearInterval(timer);
  }, [unprepared, sessionId]);
  const reported = useRef(onSession);
  reported.current = onSession;
  useEffect(() => {
    if (session) reported.current?.(session);
  }, [session]);

  // Restarting a range-based session asks the model for fresh questions on the same range.
  const start = async () => {
    setBusy(true);
    setRestarting(true);
    setError(null);
    try {
      const src = session?.source;
      // a new reading is written in the language of 設定 › 導讀, which the book takes first
      if (src) await api.updateMaterial(src.material_id, { translate_zh: guideLang === "zh" });
      const s = src ? await api.startFromMaterial(src.material_id, src.start, src.end) : await api.startSession("correlation-causation", "rules-v1");
      setAnswer("");
      setLast(null);
      onSessionChange(s.id);
    } catch (e) {
      setError((e as RequestError).body);
    } finally {
      setBusy(false);
      setRestarting(false);
    }
  };

  const submitCommand = async (s: SessionView) => {
    setBusy(true);
    setError(null);
    try {
      const cmd = failedAnswer
        ? await cmds.send("retry", { target_command_id: failedAnswer })
        : await cmds.send("answer", { answer: answer.trim() }, { question_id: s.current_question?.id, expected_revision: s.revision });
      if (cmd.status === "completed") {
        setFailedAnswer(null);
        if (cmd.result?.message) setLast(cmd.result.message);
        setAnswer("");
      } else {
        setFailedAnswer(cmd.command_id);
        setError({ ...(cmd.error ?? { code: "failed", detail: "" }), retryable: cmd.retryable });
      }
    } catch (e) {
      const body = e instanceof CommandRefused ? e.body : (e as RequestError).body;
      if (body?.session) setSession(body.session);
      setError(body);
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!session || !answer.trim()) return;
    if (isGraph(session)) return submitCommand(session);
    setBusy(true);
    setError(null);
    try {
      const r = await api.answer(session.id, answer.trim());
      setSession(r.session);
      setLast(r.message);
      setAnswer(""); // only cleared on success — on error the student's text stays
    } catch (e) {
      const err = (e as RequestError).body;
      setError(err);
      if (err.turn) api.session(session.id).then(setSession).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  if (!session && embedded) return error ? <div className="error">{error.detail}</div> : <p className="muted">{t("material.loading")}</p>;
  if (!session) {
    return (
      <div className="panel">
        <h2>{t("learn.demo.title")}</h2>
        <p className="muted">
          {t("learn.demo.intro")}
        </p>
        {error && <div className="error">{error.detail}</div>}
        <button className="primary" onClick={start} disabled={busy}>
          {t("learn.start")}
        </button>
      </div>
    );
  }

  const q = session.current_question;
  const mc = session.model_config_snapshot;
  const src = session.source;
  const concepts = Object.fromEntries(session.concepts.map((c) => [c.id, c.title]));

  const reading = session.reading && (view === "guide" || !session.reading.done);
  if (embedded && view === "guide" && session.reading) {
    return <ReadingPanel session={session} onSession={setSession} continuous />;
  }

  return (
    <>
      {embedded ? (
        quiz && (
          <div className="row actions">
            <Progress session={session} concepts={concepts} />
          </div>
        )
      ) : (
      <div className="doc-head">
        <div className="doc-meta">
          {src ? (
            <button className="link" onClick={() => onOpenMaterial?.(src.material_id)}>
              {src.material_title} · {src.label}
            </button>
          ) : (
            <span>{t("learn.demoMaterial", { version: session.content_version })}</span>
          )}
          <span>{t("learn.strategy", { id: session.strategy_id })}</span>
          <BackendBadge active={{ backend: mc.backend, model: mc.resolved_model ?? mc.model, simulated: !!mc.simulated }} />
        </div>
        <h1 className="doc-title">{session.content_title}</h1>
        <div className="row actions">
          <Progress session={session} concepts={concepts} />
          <span className="spacer" />
          {session.status !== "closed" && (
            <button className="ghost" onClick={start} disabled={busy}>
              {t(restarting ? "learn.restart.preparing" : src ? "learn.restart.sameRange" : "learn.restart")}
            </button>
          )}
          {restarting && <Working label={t("learn.restart.working")} />}
        </div>
      </div>
      )}

      {session.status === "closed" ? (
        <div className="panel notice" data-testid="closed">
          <p>{t("learn.closed")}</p>
        </div>
      ) : reading ? (
        <ReadingPanel session={session} onSession={setSession} autoFinish={autoQuiz} onAutoFinished={onAutoQuizDone} continuous={embedded} />
      ) : session.status === "completed" && session.summary ? (
        <div className="panel" data-testid="summary">
          <h2>{t("learn.summary.title", { verdict: t(session.summary.verdict === "passed" ? "learn.summary.allPassed" : "learn.summary.needsReview") })}</h2>
          <div className="grid2">
            <div>
              <strong>{t("learn.summary.practiced")}</strong>
              <ul>
                {session.summary.practiced.map((p) => (
                  <li key={p.concept_id}>{t("learn.summary.practicedItem", { title: p.title, status: p.status })}</li>
                ))}
              </ul>
            </div>
            <div>
              <strong>{t("learn.summary.stuck")}</strong>
              <ul>{session.summary.stuck_at.length ? session.summary.stuck_at.map((s) => <li key={s.question_id}>{s.prompt}</li>) : <li>{t("learn.summary.notStuck")}</li>}</ul>
            </div>
            <div>
              <strong>{t("learn.summary.reviewNext")}</strong>
              <ul>{session.summary.review_next.length ? session.summary.review_next.map((r) => <li key={r.concept_id}>{r.title}</li>) : <li>{t("learn.summary.noReview")}</li>}</ul>
            </div>
          </div>
          <div className="row">
            <a href={api.exportUrl(session.id, "md")} download>
              {t("common.exportMarkdown")}
            </a>
            <a href={api.exportUrl(session.id, "json")} download>
              {t("common.exportJson")}
            </a>
          </div>
        </div>
      ) : (
        q && (
          <div className="panel">
            <div className="muted">
              {t("learn.question.meta", {
                concept: q.concept_title,
                type: t(q.type === "diagnostic" ? "learn.question.diagnostic" : "learn.question.transfer"),
                used: q.hints_used,
                total: q.hints_total,
              })}
            </div>
            <div className="question" data-testid="question">
              <RichText text={q.prompt} />
            </div>
            {last && <TutorBubble message={last} />}
            {error && (
              <div className="error" data-testid="error">
                {error.detail}
                {error.retryable && <div className="muted">{t("learn.answerKept")}</div>}
              </div>
            )}
            <div style={{ marginTop: 12 }}>
              <textarea
                aria-label={t("learn.answer.label")}
                placeholder={t("learn.answer.placeholder")}
                value={answer}
                onChange={(e) => {
                  setAnswer(e.target.value);
                  setFailedAnswer(null); // an edited answer is a new answer, not a retry of the failed one
                }}
                disabled={busy}
              />
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <button className="primary" onClick={submit} disabled={busy || !answer.trim()}>
                {t(busy ? "learn.judging" : error?.retryable ? "learn.retrySubmit" : "learn.submit")}
              </button>
              {busy && !restarting && <Working label={t("learn.judging.working")} />}
              <button onClick={() => setShowSource((v) => !v)}>{t(showSource ? "learn.source.hide" : src ? "learn.source.range" : "learn.source.concept")}</button>
            </div>
            {showSource && <SourcePanel session={session} conceptId={q.concept_id} />}
          </div>
        )
      )}

      {session.turns.length > 0 && (
      <div className="panel">
        <h2>{t(session.status !== "active" ? "learn.turns.completed" : "learn.turns.previous")}</h2>
        {/* while active, the latest tutor message is already shown under the question */}
        <TurnList turns={session.status !== "active" || !last ? session.turns : session.turns.slice(0, -1)} questions={session.questions} />
      </div>
      )}
    </>
  );
}

function lastMessage(s: SessionView): TutorMessage | null {
  for (let i = s.turns.length - 1; i >= 0; i--) {
    const m = s.turns[i].tutor_message;
    if (m) return m;
  }
  return null;
}

/** For a range-based session the source is the original text of the range; for the demo it is the concept write-up. */
function SourcePanel({ session, conceptId }: { session: SessionView; conceptId: string }) {
  const t = useT();
  if (session.source) {
    return (
      <div className="tutor source" style={{ marginTop: 12 }}>
        <div className="label">
          {session.source.material_title} · {session.source.label}
        </div>
        <div className="source-text">{session.source.text}</div>
      </div>
    );
  }
  const text = session.concepts.find((x) => x.id === conceptId);
  if (!text) return null;
  return (
    <div className="tutor" style={{ marginTop: 12 }}>
      <div className="label">{t("learn.source.label", { id: conceptId })}</div>
      <strong>{text.title}</strong>
      <RichText text={text.explanation} />
      <ul>
        {text.examples.map((e, i) => (
          <li key={i}>
            <RichText text={e} inline />
          </li>
        ))}
      </ul>
    </div>
  );
}
