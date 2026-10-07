import { useEffect, useState } from "react";
import { api, RequestError } from "../api";
import type { ApiError, Guide, SessionView } from "../types";
import { RichText } from "./RichText";
import { Working } from "./Working";
import { useT, type Key } from "../i18n";
import { CommandRefused, isGraph, useCommands } from "../workflow";

interface Props {
  session: SessionView;
  onSession: (s: SessionView) => void;
}

const POLL_MS = 2000;

/** 導讀: one unit at a time. Every action is a command of the reading workflow (resent safely, resumed after a
 *  reload); the workflow writes and checks the guides on its own and the panel polls the session while it works.
 *  The student moves on only by saying they understood, can ask about the current unit first, and can go to the
 *  questions early. A session left by the removed old flow is shown read-only. */
// student-facing headline of a held guide / quiz (the check record with claim ids stays folded below it)
const ERROR_HEADLINE: Record<string, Key> = {
  notation_unverified: "reading.error.notation_unverified",
  source_unverified: "reading.error.source_unverified",
  quality_check_failed: "reading.error.quality_check_failed",
  context_overflow: "reading.error.context_overflow",
  context_missing: "reading.error.context_missing",
  review_invalid: "reading.error.review_invalid",
  quiz_basis_insufficient: "reading.error.quiz_basis_insufficient",
  quiz_unverified: "reading.error.quiz_unverified",
};

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
  const isLast = rd.index + 1 >= rd.total;
  const busy = !!job && !job.error;
  const readOnly = !isGraph(session); // created by the removed old flow: viewable, not continued
  const wf = session.workflow ?? null;
  const cmds = useCommands(session, onSession, ["ask", "next", "finish", "retry", "guide", "retry_blocks"]);
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
    const target = wf?.waiting === "retry" ? wf.failed_command_id : failedAsk;
    if (!target) return;
    if (target === failedAsk) return askRetry(target);
    command("retry", { target_command_id: target });
  };

  const next = async () => {
    const cmd = await command("next");
    if (cmd?.status === "completed") {
      setQuestion("");
      setShowOriginal(false);
    }
  };

  const finish = async () => {
    setConfirmFinish(false);
    await command("finish");
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
    setAsking(true);
    const cmd = await command("ask", { question: question.trim() });
    setAsking(false);
    if (cmd?.status === "completed") {
      setQuestion(""); // only cleared on success — on failure the question stays and can be retried
      setFailedAsk(null);
    } else if (cmd) setFailedAsk(cmd.command_id);
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
      {rd.index === 0 && (rd.segmentation?.skipped?.length ?? 0) > 0 && (
        <details className="skipped" data-testid="skipped">
          <summary>{t("reading.skipped.summary", { n: rd.segmentation!.skipped!.length })}</summary>
          {rd.segmentation!.skipped!.map((x, i) => (
            <details key={i}>
              <summary>
                {t(`reading.skipped.role.${x.role}` as Key)}
                <span className="muted"> · {x.label}</span>
              </summary>
              <div className="source-text">{x.text}</div>
            </details>
          ))}
        </details>
      )}
      {quote.heading && <h2 className="reading-heading">{quote.heading}</h2>}

      {guide?.blocks ? (
        <TopicBlocks guide={guide} retrying={sending} onRetry={!readOnly && !busy ? () => command("retry_blocks") : undefined} />
      ) : (
      <>
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
          {guide.unverified && guide.unverified.length > 0 && (
            <details className="unverified" data-testid="unverified">
              <summary>{t("reading.unverified.summary", { n: guide.unverified.length })}</summary>
              <ul>
                {guide.unverified.map((u, i) => (
                  <li key={i}>{t("reading.unverified.coverage", { source: u.source })}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      ) : (
        !shownError && !readOnly && (
          <div className="tutor" data-testid="guide-loading">
            <div className="label">{t("common.tutor")}</div>
            <Working stage={segmenting ? "segment" : (job?.stage ?? "guide")} detail={job?.detail_stage} />
          </div>
        )
      )}
      </>
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
          {ERROR_HEADLINE[shownError.code] ? (
            <>
              <div data-testid="error-headline">{t(ERROR_HEADLINE[shownError.code])}</div>
              <details style={{ marginTop: 4 }}>
                <summary>{t("reading.error.details")}</summary>
                {shownError.detail}
              </details>
            </>
          ) : (
            shownError.detail
          )}
          {shownError.retryable === false && <div style={{ marginTop: 4 }}>{t("reading.error.noRetry")}</div>}
          <div style={{ marginTop: 6 }}>
            {(wf?.waiting === "retry" || failedAsk) && shownError.retryable !== false && (
              <button onClick={retry} disabled={sending || asking}>{t("reading.retry")}</button>
            )}
          </div>
        </div>
      )}

      {readOnly && (
        <div className="muted" data-testid="read-only">
          {t("reading.readOnly")}
        </div>
      )}

      {guide && !busy && !readOnly && (
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

/** A 主題 written one 區塊 at a time: each source 區塊, then its guide. A 區塊 whose guide did not pass its checks
 *  shows the source only; those 區塊 can be written again without touching the others. */
function TopicBlocks({ guide, retrying, onRetry }: { guide: Guide; retrying: boolean; onRetry?: () => void }) {
  const t = useT();
  const blocks = guide.blocks ?? [];
  return (
    <div className="topic" data-testid="topic">
      {guide.topic_summary && (
        <p className="topic-summary" data-testid="topic-summary">
          <span className="label">{t("reading.topic.summary")}</span>
          {guide.topic_summary}
        </p>
      )}
      {blocks.map((b) => (
        <section key={b.index} className="block" data-testid="block">
          {b.heading && <h3 className="block-heading">{b.heading}</h3>}
          <blockquote className="quote">
            <div className="label">{t("reading.block.label", { n: b.index + 1, total: blocks.length })}</div>
            <div className="source-text">
              <RichText text={b.source} />
            </div>
            {b.chinese_text && (
              <div className="chinese">
                <strong>{t("reading.chineseText")}</strong>
                <RichText text={b.chinese_text} />
              </div>
            )}
          </blockquote>
          {b.status === "accepted" ? (
            <div className="guide" data-testid="block-guide">
              <RichText text={b.guide} />
              {b.unverified && b.unverified.length > 0 && (
                <details className="unverified" data-testid="unverified">
                  <summary>{t("reading.unverified.summary", { n: b.unverified.length })}</summary>
                  <ul>
                    {b.unverified.map((u, i) => (
                      <li key={i}>{t("reading.unverified.coverage", { source: u.source })}</li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          ) : (
            <div className="block-failed" data-testid="block-failed">
              {t("reading.block.failed")}
              {(b.attempt ?? 1) > 1 && <> {t("reading.block.retried", { n: b.attempt ?? 1 })}</>}
            </div>
          )}
        </section>
      ))}
      {guide.partial && onRetry && (
        <div className="row">
          <button onClick={onRetry} disabled={retrying} data-testid="retry-blocks">
            {t("reading.block.retry")}
          </button>
        </div>
      )}
      {guide.check && (
        <div className="guide">
          <div className="check">
            <span className="label">{t("reading.check")}</span>
            <RichText text={guide.check} inline />
          </div>
        </div>
      )}
    </div>
  );
}
