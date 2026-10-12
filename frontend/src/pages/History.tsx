// 學習紀錄: every reading, 導讀 window and practice, newest first and grouped by day. A record opens in place of the
// list (a back link returns to it), with its progress, its 導讀 section by section, the answers turn by turn, and the
// settings it ran with folded away at the end.
import { useEffect, useRef, useState } from "react";
import { api, RequestError } from "../api";
import type { SessionListItem, SessionView } from "../types";
import { TurnList } from "../components/TurnList";
import { Progress } from "../components/Progress";
import { RichText } from "../components/RichText";
import { Icon } from "../components/Icons";
import { useLocale, useT, type T } from "../i18n";

type Kind = "tour" | "reading" | "quiz" | "demo";

function kindOf(s: SessionListItem | SessionView): Kind {
  if ("progress" in s ? s.tour : s.workflow?.waiting === "tour") return "tour";
  if (!s.source) return "demo";
  const stage = "progress" in s ? s.progress.stage : s.reading && !s.reading.done && s.state.phase === "reading" ? "reading" : "quiz";
  return stage === "reading" && s.status === "active" ? "reading" : "quiz";
}

const KIND_KEY = { tour: "history.kind.tour", reading: "history.kind.reading", quiz: "history.kind.quiz", demo: "history.kind.demo" } as const;

/** The record's own name and the book it comes from: a title that only repeats the book's name gives its range. */
function nameOf(s: SessionListItem | SessionView, t: T): { title: string; book: string } {
  const book = s.source?.material_title ?? t("common.demoVersion", { version: s.content_version });
  let title = s.content_title.trim();
  if (s.source && title.startsWith(s.source.material_title)) title = title.slice(s.source.material_title.length).trim();
  return { title: title || s.source?.label || s.content_title, book };
}

function dayKey(d: Date) {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function useDates() {
  const t = useT();
  const locale = useLocale() === "en" ? "en" : "zh-TW";
  const time = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  const day = new Intl.DateTimeFormat(locale, { month: "long", day: "numeric", weekday: "short" });
  const full = new Intl.DateTimeFormat(locale, { year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  const dayLabel = (d: Date) => {
    const today = new Date();
    const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    if (dayKey(d) === dayKey(today)) return t("history.today");
    if (dayKey(d) === dayKey(yesterday)) return t("history.yesterday");
    const label = day.format(d);
    return d.getFullYear() === today.getFullYear() ? label : `${d.getFullYear()} · ${label}`;
  };
  return { time: (d: Date) => time.format(d), full: (d: Date) => full.format(d), dayLabel };
}

/** Where a record stands: the verdict once it is done, how far it is while it runs. */
function Standing({ s }: { s: SessionListItem }) {
  const t = useT();
  if (s.status === "completed")
    return <span className={`hist-pill ${s.verdict === "passed" ? "ok" : "warn"}`}>{t(s.verdict === "passed" ? "session.verdict.passed" : "session.verdict.review")}</span>;
  if (s.status === "closed") return <span className="hist-pill" title={t("learn.closed")}>{t("session.status.closed")}</span>;
  const { read, total, stage } = s.progress;
  if (stage === "quiz" && !s.tour) return <span className="hist-pill on">{t("history.progress.quiz")}</span>;
  const p = total > 0 ? Math.min(read / total, 1) : 0;
  return (
    <span className="hist-meter" title={t(s.tour ? "history.progress.guides" : "history.progress.reading", { n: read, total })}>
      <span className="hist-meter-bar" aria-hidden="true">
        <span style={{ width: `${Math.round(p * 100)}%` }} />
      </span>
      <span className="hist-meter-text">
        {read}/{total}
      </span>
    </span>
  );
}

export function HistoryPage({ onOpen }: { onOpen: (id: string) => void }) {
  const t = useT();
  const dates = useDates();
  const [list, setList] = useState<SessionListItem[] | null>(null);
  const [selected, setSelected] = useState<SessionView | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const menuBox = useRef<HTMLDivElement | null>(null);

  const reload = () => api.sessions().then(setList).catch((e: RequestError) => setMsg(e.body.detail));
  useEffect(() => {
    reload();
  }, []);

  useEffect(() => {
    if (!menuFor) return;
    const onDown = (e: MouseEvent) => !menuBox.current?.contains(e.target as Node) && setMenuFor(null);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenuFor(null);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuFor]);

  const open = async (id: string) => {
    setMenuFor(null);
    setOpening(id);
    try {
      setSelected(await api.session(id));
      window.scrollTo?.({ top: 0 });
    } catch (e) {
      setMsg((e as RequestError).body?.detail ?? String(e));
    } finally {
      setOpening(null);
    }
  };

  const replay = async (id: string) => {
    setMenuFor(null);
    setMsg(t("history.replaying"));
    try {
      const r = await api.replay(id);
      const vars = { replayed: r.replayed_answers, total: r.total_answers, id: r.session.id };
      setMsg(r.error ? t("history.replay.doneInterrupted", { ...vars, detail: r.error.detail }) : t("history.replay.done", vars));
      await reload();
      await open(r.session.id);
    } catch (e) {
      setMsg((e as RequestError).body.detail);
    }
  };

  const remove = async (id: string) => {
    setConfirmDelete(null);
    try {
      await api.deleteSession(id);
      if (selected?.id === id) setSelected(null);
      setMsg(t("history.delete.done"));
      await reload();
    } catch (e) {
      setMsg((e as RequestError).body.detail);
    }
  };

  const notice = msg && (
    <p className="hist-msg" role="status">
      <span>{msg}</span>
      <button type="button" className="icon-btn" aria-label={t("common.close")} onClick={() => setMsg(null)}>
        <Icon.close />
      </button>
    </p>
  );

  if (selected) {
    return (
      <div className="hist">
        <button type="button" className="hist-back" onClick={() => setSelected(null)}>
          <Icon.chevron />
          {t("history.title")}
        </button>
        {notice}
        <Record
          s={selected}
          confirming={confirmDelete === selected.id}
          onContinue={() => onOpen(selected.id)}
          onReplay={() => replay(selected.id)}
          onDelete={() => setConfirmDelete(selected.id)}
          onConfirmDelete={() => remove(selected.id)}
          onCancelDelete={() => setConfirmDelete(null)}
          when={dates.full(new Date(selected.created_at))}
        />
      </div>
    );
  }

  // newest first, one group per day
  const groups: { label: string; items: SessionListItem[] }[] = [];
  for (const s of list ?? []) {
    const d = new Date(s.created_at);
    const label = dates.dayLabel(d);
    if (groups.at(-1)?.label === label) groups.at(-1)!.items.push(s);
    else groups.push({ label, items: [s] });
  }

  return (
    <div className="hist">
      <header className="hist-head">
        <h1 className="doc-title">{t("history.title")}</h1>
        <p className="hist-sub">{list && list.length > 0 ? t("history.count", { count: list.length }) : t("history.subtitle")}</p>
      </header>
      {notice}
      {list && list.length === 0 && (
        <div className="hist-empty">
          <Icon.history />
          <p>{t("history.empty")}</p>
        </div>
      )}
      {groups.map((g) => (
        <section key={g.label} className="hist-day" aria-label={g.label}>
          <h2 className="hist-day-label">{g.label}</h2>
          <ul className="hist-list">
            {g.items.map((s) => {
              const kind = kindOf(s);
              const { title, book } = nameOf(s, t);
              const confirming = confirmDelete === s.id;
              return (
                <li key={s.id} className={`hist-item ${confirming ? "confirming" : ""} ${opening === s.id ? "busy" : ""}`}>
                  <button type="button" className="hist-open" onClick={() => open(s.id)} title={s.content_title}>
                    <span className={`hist-kind ${kind}`} aria-hidden="true">
                      {kind === "quiz" ? <Icon.quiz /> : kind === "demo" ? <Icon.learn /> : <Icon.book />}
                    </span>
                    <span className="hist-main">
                      <span className="hist-title">{title}</span>
                      <span className="hist-meta">
                        <span className="hist-book">{book}</span>
                        {kind !== "demo" && (
                          <>
                            <span className="hist-dot" aria-hidden="true">·</span>
                            <span>{t(KIND_KEY[kind])}</span>
                          </>
                        )}
                        <span className="hist-dot" aria-hidden="true">·</span>
                        <time dateTime={s.created_at}>{dates.time(new Date(s.created_at))}</time>
                        {s.simulated && <span className="hist-pill sim">{t("history.simulated")}</span>}
                      </span>
                    </span>
                    <span className="hist-standing">
                      <Standing s={s} />
                    </span>
                  </button>
                  <div className="hist-actions">
                    {confirming ? (
                      <>
                        <span className="hist-confirm-text">{t("history.delete.confirmText")}</span>
                        <button type="button" className="sm danger" onClick={() => remove(s.id)}>
                          {t("history.delete.confirm")}
                        </button>
                        <button type="button" className="sm" onClick={() => setConfirmDelete(null)}>
                          {t("common.cancel")}
                        </button>
                      </>
                    ) : (
                      <>
                        {s.status === "active" && (
                          <button type="button" className="sm hist-continue" onClick={() => onOpen(s.id)}>
                            {t("common.continue")}
                          </button>
                        )}
                        <div className="hist-more" ref={menuFor === s.id ? menuBox : undefined}>
                          <button
                            type="button"
                            className="icon-btn"
                            aria-label={t("history.more")}
                            aria-haspopup="menu"
                            aria-expanded={menuFor === s.id}
                            onClick={() => setMenuFor((v) => (v === s.id ? null : s.id))}
                          >
                            <Icon.more />
                          </button>
                          {menuFor === s.id && (
                            <div className="menu hist-menu" role="menu">
                              {!s.tour && (
                                <button type="button" role="menuitem" onClick={() => replay(s.id)}>
                                  {t("history.replay")}
                                </button>
                              )}
                              <a role="menuitem" href={api.exportUrl(s.id, "md")} download onClick={() => setMenuFor(null)}>
                                {t("common.exportMarkdown")}
                              </a>
                              <a role="menuitem" href={api.exportUrl(s.id, "json")} download onClick={() => setMenuFor(null)}>
                                {t("common.exportJson")}
                              </a>
                              <div className="menu-sep" />
                              <button
                                type="button"
                                role="menuitem"
                                className="danger"
                                onClick={() => {
                                  setMenuFor(null);
                                  setConfirmDelete(s.id);
                                }}
                              >
                                {t("history.delete")}
                              </button>
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** One record opened: what it is and where it stands, then its 導讀 and its answers. */
function Record({ s, when, confirming, onContinue, onReplay, onDelete, onConfirmDelete, onCancelDelete }: {
  s: SessionView;
  when: string;
  confirming: boolean;
  onContinue: () => void;
  onReplay: () => void;
  onDelete: () => void;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
}) {
  const t = useT();
  const kind = kindOf(s);
  const { title, book } = nameOf(s, t);
  const rd = s.reading;
  const cfg = s.model_config_snapshot;
  const errors = s.turns.filter((x) => x.error).length;
  const status =
    s.status === "completed"
      ? { text: t(s.summary?.verdict === "passed" ? "session.verdict.passed" : "session.verdict.review"), cls: s.summary?.verdict === "passed" ? "ok" : "warn" }
      : s.status === "closed"
        ? { text: t("session.status.closed"), cls: "" }
        : { text: t("session.status.active"), cls: "on" };
  const guided = rd ? Object.keys(rd.guides).length : 0;
  const review = s.summary?.review_next ?? [];

  return (
    <article className="hist-record">
      <header className="hist-record-head">
        <p className="hist-eyebrow">
          <span className={`hist-kind sm ${kind}`} aria-hidden="true">
            {kind === "quiz" ? <Icon.quiz /> : kind === "demo" ? <Icon.learn /> : <Icon.book />}
          </span>
          {t(KIND_KEY[kind])}
          <span className="hist-dot" aria-hidden="true">·</span>
          <time dateTime={s.created_at}>{when}</time>
        </p>
        <h1 className="doc-title">{title}</h1>
        <p className="hist-record-book">{s.source ? `${book} · ${s.source.label}` : book}</p>
        <div className="hist-record-actions">
          {confirming ? (
            <>
              <span className="hist-confirm-text">{t("history.delete.confirmText")}</span>
              <button type="button" className="sm danger" onClick={onConfirmDelete}>
                {t("history.delete.confirm")}
              </button>
              <button type="button" className="sm" onClick={onCancelDelete}>
                {t("common.cancel")}
              </button>
            </>
          ) : (
            <>
              {s.status === "active" && (
                <button type="button" className="sm hist-continue" onClick={onContinue}>
                  {t("common.continue")}
                </button>
              )}
              <a className="hist-link" href={api.exportUrl(s.id, "md")} download>
                <Icon.download />
                {t("common.exportMarkdown")}
              </a>
              <a className="hist-link" href={api.exportUrl(s.id, "json")} download>
                <Icon.download />
                {t("common.exportJson")}
              </a>
              <button type="button" className="sm ghost" onClick={onReplay}>
                {t("history.replay")}
              </button>
              <button type="button" className="sm ghost hist-delete" onClick={onDelete}>
                <Icon.trash />
                {t("history.delete")}
              </button>
            </>
          )}
        </div>
      </header>

      <dl className="hist-stats">
        <div>
          <dt>{t("history.stat.status")}</dt>
          <dd>
            <span className={`hist-pill ${status.cls}`}>{status.text}</span>
          </dd>
        </div>
        {rd && (
          <div>
            <dt>{t("history.stat.reading")}</dt>
            <dd>
              {guided}
              <small>/{rd.total}</small>
            </dd>
          </div>
        )}
        <div>
          <dt>{t("history.stat.turns")}</dt>
          <dd>{s.summary?.turn_count ?? s.turns.length}</dd>
        </div>
        <div>
          <dt>{t("history.stat.errors")}</dt>
          <dd className={(s.summary?.error_count ?? errors) > 0 ? "bad" : ""}>{s.summary?.error_count ?? errors}</dd>
        </div>
      </dl>

      {kind !== "tour" && (s.state.concept_order.length > 0 || rd) && (
        <section className="hist-section">
          <h2>{t("history.progress.title")}</h2>
          <Progress session={s} concepts={Object.fromEntries(s.concepts.map((c) => [c.id, c.title]))} />
          {review.length > 0 && (
            <p className="hist-review">
              <span>{t("history.review")}</span>
              {review.map((r) => r.title).join(t("common.listSeparator"))}
            </p>
          )}
        </section>
      )}

      {rd && rd.quotes.length > 0 && (
        <section className="hist-section">
          <h2>
            {t("history.reading.title", { total: rd.total })}
            {rd.stopped_at != null && <small>{t("history.reading.stoppedEarly", { n: rd.stopped_at + 1 })}</small>}
          </h2>
          <ol className="hist-quotes" data-testid="reading-log">
            {rd.quotes.map((q) => {
              const qa = rd.qa[String(q.index)] ?? [];
              const g = rd.guides[String(q.index)];
              return (
                <li key={q.id}>
                  {!g && qa.length === 0 ? (
                    // not guided and nothing asked: nothing to unfold
                    <div className="hist-quote-row">
                      <span className="hist-quote-no unread">{q.index + 1}</span>
                      <span className="hist-quote-name">{q.heading ?? q.label}</span>
                      <span className="hist-quote-meta">
                        <span className="hist-pill">{t("history.reading.notGuided")}</span>
                      </span>
                    </div>
                  ) : (
                    <details>
                      <summary>
                        <span className="hist-quote-no">{q.index + 1}</span>
                        <span className="hist-quote-name">{q.heading ?? q.label}</span>
                        <span className="hist-quote-meta">
                          {q.heading ? q.label : null}
                          {!g && <span className="hist-pill">{t("history.reading.notGuided")}</span>}
                          {qa.length > 0 && <span className="hist-pill on">{t("history.reading.asked", { count: qa.length })}</span>}
                        </span>
                        <Icon.chevron />
                      </summary>
                      <div className="hist-quote-body">
                        {g && <RichText text={g.guide} />}
                        {qa.map((x, i) => (
                          <div key={i} className="hist-qa">
                            <p className="hist-qa-q">{t("common.youAsked", { question: x.question })}</p>
                            <RichText text={x.answer} />
                          </div>
                        ))}
                      </div>
                    </details>
                  )}
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {(s.turns.length > 0 || kind !== "tour") && (
        <section className="hist-section hist-turns">
          <h2>{t("history.turns")}</h2>
          <TurnList turns={s.turns} showDetails questions={s.questions} />
        </section>
      )}

      <details className="hist-tech">
        <summary>{t("history.tech")}</summary>
        <dl>
          <dt>{t("history.tech.strategy")}</dt>
          <dd>
            {s.strategy_id} v{s.strategy_version}
          </dd>
          <dt>{t("history.tech.model")}</dt>
          <dd>
            {cfg.backend} / {cfg.resolved_model ?? cfg.model} · temp {cfg.temperature} · ctx {cfg.context_tokens}
          </dd>
          <dt>{t("history.tech.id")}</dt>
          <dd>
            <code>{s.id}</code>
          </dd>
        </dl>
      </details>
    </article>
  );
}
