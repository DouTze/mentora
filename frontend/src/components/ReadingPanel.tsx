import { useEffect, useRef, useState } from "react";
import { api, RequestError } from "../api";
import type { ApiError, SessionView } from "../types";
import { RichText } from "./RichText";
import { Working } from "./Working";
import { useT } from "../i18n";
import { CommandRefused, isGraph, useCommands } from "../workflow";

interface Props {
  session: SessionView;
  onSession: (s: SessionView) => void;
}

const POLL_MS = 2000;

/** 導讀: one quote at a time. Model work (segmenting, writing a guide, compacting a part, writing the questions)
 *  runs as a background job on the server; this panel asks for it, then polls the session every 2 s until
 *  `reading.job` is gone. The student moves on only by saying they understood, can ask about the current
 *  quote first, and can go to the questions early. */
export function ReadingPanel({ session, onSession }: Props) {
  const t = useT();
  const rd = session.reading!;
  const quote = rd.quotes[rd.index];
  const guide = rd.guides[String(rd.index)];
  const qa = rd.qa[String(rd.index)] ?? [];
  const job = rd.job ?? null;
  const segmenting = rd.segmentation?.status === "pending";
  const [asking, setAsking] = useState(false);
  const [question, setQuestion] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [showOriginal, setShowOriginal] = useState(false);
  const [showPrevious, setShowPrevious] = useState(false);
  const [confirmFinish, setConfirmFinish] = useState(false);
  const requested = useRef<string | null>(null); // "<session>:<index>" for which the guide was asked
  const isLast = rd.index + 1 >= rd.total;
  const busy = !!job && !job.error;
  // LangGraph sessions: every action is a command with its own id (resent safely, resumed after a reload)
  const graph = isGraph(session);
  const wf = session.workflow ?? null;
  const cmds = useCommands(session, onSession, ["ask", "next", "finish", "retry", "guide"]);
  const [failedAsk, setFailedAsk] = useState<string | null>(null);
  const sending = !!cmds.running && cmds.running.type !== "ask";

  const command = async (type: string, payload: Record<string, unknown> = {}) => {
    setError(null);
    try {
      const cmd = await cmds.send(type, payload, { unit_id: quote.unit_id, expected_revision: session.revision });
      if (cmd.status === "failed" && cmd.error && !(wf?.waiting === "retry")) setError({ ...cmd.error, retryable: cmd.retryable });
      return cmd;
    } catch (e) {
      const body = e instanceof CommandRefused ? e.body : (e as RequestError).body;
      if (body?.session) onSession(body.session);
      setError(body);
      return null;
    }
  };

  // ask for the guide of the current quote (starts the job, or returns it when cached)
  useEffect(() => {
    const key = `${session.id}:${rd.index}`;
    if (graph) return; // the workflow writes the guide on its own; the page only polls
    if (guide || segmenting) return; // while segmenting, only poll; the guide is asked for once the quotes are final
    if ((job && !job.error) || requested.current === key) return;
    requested.current = key;
    setError(null);
    api
      .readingGuide(session.id)
      .then((r) => onSession(r.session))
      .catch((e: RequestError) => setError(e.body));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id, rd.index, guide, segmenting]);

  // poll while the server is working
  useEffect(() => {
    if (!busy && !segmenting) return;
    const t = setInterval(() => {
      api
        .session(session.id)
        .then(onSession)
        .catch(() => {});
    }, POLL_MS);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id, busy, segmenting]);

  const retry = () => {
    if (graph) {
      const target = wf?.waiting === "retry" ? wf.failed_command_id : failedAsk;
      if (!target) return;
      if (target === failedAsk) return askRetry(target);
      command("retry", { target_command_id: target });
      return;
    }
    requested.current = null;
    setError(null);
    api
      .readingGuide(session.id)
      .then((r) => onSession(r.session))
      .catch((e: RequestError) => setError(e.body));
  };

  const next = async () => {
    if (graph) {
      const cmd = await command("next");
      if (cmd?.status === "completed") {
        setQuestion("");
        setShowOriginal(false);
      }
      return;
    }
    setError(null);
    try {
      const r = await api.readingNext(session.id);
      setQuestion("");
      setShowOriginal(false);
      onSession(r.session);
    } catch (e) {
      setError((e as RequestError).body);
    }
  };

  const finish = async () => {
    setConfirmFinish(false);
    if (graph) {
      await command("finish");
      return;
    }
    setError(null);
    try {
      const r = await api.readingFinish(session.id);
      onSession(r.session);
    } catch (e) {
      setError((e as RequestError).body);
    }
  };

  const askRetry = async (target: string) => {
    setAsking(true);
    const cmd = await command("retry", { target_command_id: target });
    setAsking(false);
    if (cmd?.status === "completed") {
      setFailedAsk(null);
      setQuestion("");
    }
  };

  const ask = async () => {
    if (!question.trim()) return;
    if (graph) {
      setAsking(true);
      const cmd = await command("ask", { question: question.trim() });
      setAsking(false);
      if (cmd?.status === "completed") {
        setQuestion(""); // only cleared on success — on failure the question stays and can be retried
        setFailedAsk(null);
      } else if (cmd) setFailedAsk(cmd.command_id);
      return;
    }
    setAsking(true);
    setError(null);
    try {
      const r = await api.readingAsk(session.id, question.trim());
      setQuestion("");
      onSession(r.session);
    } catch (e) {
      setError((e as RequestError).body);
    } finally {
      setAsking(false);
    }
  };

  const partEnds = rd.parts.length > 1 && rd.parts[quote.part]?.quote_to === rd.index;
  const shownError = job?.error ?? error;

  return (
    <div className="panel reading" data-testid="reading">
      <div className="meta">
        {t("reading.meta", { n: rd.index + 1, total: rd.total, label: quote.label })}
        {rd.parts.length > 1 && ` · ${t("reading.meta.part", { part: quote.part + 1, parts: rd.parts.length })}`}
        {rd.segmentation && !segmenting && (
          <span title={rd.segmentation.notes.join("\n") || undefined}>
            {" · "}
            {rd.segmentation.planner === "model" && rd.segmentation.notes.length === 0 ? t("reading.meta.segModel", { total: rd.total }) : t("reading.meta.segParagraph", { total: rd.total })}
          </span>
        )}
      </div>
      {quote.heading && <h2 className="reading-heading">{quote.heading}</h2>}

      <blockquote className="quote">
        <div className="label">{t("reading.quote.label", { label: quote.label })}</div>
        <div className="excerpt">
          {t("reading.quote.open")}<RichText text={quote.excerpt} inline />{t("reading.quote.close")}
        </div>
        {guide?.chinese_text && (
          <div className="chinese">
            <strong>{t("reading.chineseText")}</strong>
            <RichText text={guide.chinese_text} />
          </div>
        )}
        <button className="link" onClick={() => setShowOriginal((v) => !v)}>
          {t(showOriginal ? "reading.original.hide" : "reading.original.show")}
        </button>
        {showOriginal && <div className="source-text">{quote.text}</div>}
      </blockquote>

      {guide ? (
        <div className="guide" data-testid="guide">
          <RichText text={guide.guide} />
          {guide.check && (
            <div className="check">
              <span className="label">{t("reading.check")}</span>
              <RichText text={guide.check} inline />
            </div>
          )}
          {guide.agent === "pi" && <div className="muted">{t("reading.byPi")}</div>}
        </div>
      ) : (
        !shownError && (
          <div className="tutor" data-testid="guide-loading">
            <div className="label">{t("common.tutor")}</div>
            <Working stage={segmenting ? "segment" : (job?.stage ?? "guide")} detail={job?.detail_stage} />
          </div>
        )
      )}

      {qa.length > 0 && (
        <div className="qa" data-testid="qa">
          {qa.map((x, i) => (
            <div key={i} className="tutor">
              <div className="label">{t("common.youAsked", { question: x.question })}</div>
              <RichText text={x.answer} />
            </div>
          ))}
        </div>
      )}

      {guide && busy && (
        <div className="tutor" data-testid="job">
          <div className="label">{t("common.tutor")}</div>
          <Working stage={job!.stage} detail={job!.detail_stage} />
        </div>
      )}

      {shownError && (
        <div className="error" data-testid="error">
          {shownError.detail}
          <div style={{ marginTop: 6 }}>
            {graph ? (
              (wf?.waiting === "retry" || failedAsk) && <button onClick={retry} disabled={sending || asking}>{t("reading.retry")}</button>
            ) : !guide ? (
              <button onClick={retry}>{t("reading.retry")}</button>
            ) : (
              <button onClick={rd.stopped_at != null ? finish : next}>{t("reading.retry")}</button>
            )}
          </div>
        </div>
      )}

      {guide && !busy && (
        <>
          <div className="composer">
            <input
              aria-label={t("reading.ask.label")}
              placeholder={t("reading.ask.placeholder")}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && ask()}
              disabled={asking}
            />
            <button onClick={ask} disabled={asking || !question.trim()}>
              {t(asking ? "reading.ask.answering" : "reading.ask.submit")}
            </button>
          </div>
          {asking && (
            <div style={{ marginTop: 8 }}>
              <Working label={t("reading.ask.working")} />
            </div>
          )}
          <div className="actions">
            <button className="primary" onClick={next} disabled={asking || sending}>
              {t(isLast ? "reading.next.last" : partEnds ? "reading.next.partEnd" : "reading.next")}
            </button>
            {!isLast &&
              (confirmFinish ? (
                <span className="row" data-testid="confirm-finish">
                  <span className="muted">{t("reading.finish.confirmText", { count: rd.total - rd.index - 1 })}</span>
                  <button onClick={finish}>{t("reading.finish.confirm")}</button>
                  <button className="ghost" onClick={() => setConfirmFinish(false)}>
                    {t("common.cancel")}
                  </button>
                </span>
              ) : (
                <button className="ghost" onClick={() => setConfirmFinish(true)} disabled={asking || sending}>
                  {t("reading.finish")}
                </button>
              ))}
            <span className="spacer" />
            {rd.index > 0 && (
              <button className="ghost" onClick={() => setShowPrevious((v) => !v)}>
                {showPrevious ? t("reading.previous.hide") : t("reading.previous.show", { count: rd.index })}
              </button>
            )}
          </div>
        </>
      )}

      {showPrevious && (
        <div className="previous" data-testid="previous">
          {rd.quotes.slice(0, rd.index).map((q) => {
            const g = rd.guides[String(q.index)];
            return (
              <details key={q.id}>
                <summary>
                  {q.index + 1}. {q.heading ?? q.label}
                  <span className="muted"> · {q.label}</span>
                </summary>
                {g ? <RichText text={g.guide} /> : <p className="muted">{t("reading.previous.notGuided")}</p>}
              </details>
            );
          })}
        </div>
      )}
    </div>
  );
}
