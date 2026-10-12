// 導讀模式: the guides of the pages around where the reader is, one 主題 after another in a column of paper, in place
// of the pages. The server prepares them on demand (app.reading.tour): pressing 導讀 at a page segments the pages
// around it and writes the guides of its 主題 first, the others after. The column scrolls on: past its first (last)
// 主題 the pages before (after) are prepared the same way. The 主題 on screen steers what is written next; leaving the
// mode stops the work.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, RequestError } from "../api";
import type { Guide, GuideNote, MaterialDetail, TourTopic, TourView, TourWindow } from "../types";
import { RichText } from "./RichText";
import { useT } from "../i18n";
import { usePrefs } from "../prefs";

const POLL_MS = 1500;
const IDLE_POLL_MS = 6000;
const FOCUS_SETTLE_MS = 350;
const EDGE_PX = 360;          // this close to the top (bottom) of the column, the pages before (after) are prepared

export const topicAnchor = (sid: string, index: number) => `tour-${sid}-${index}`;

interface Props {
  material: MaterialDetail;
  /** the window the mode was opened at (the route): brought into view, and the work turns towards it; null while
   *  the window around `at` is still being found (the mode is on screen at once, saying so) */
  session: string | null;
  /** the page (line) 導讀 was pressed at: its 主題 is brought into view first; null for a window picked in the sidebar */
  at: number | null;
  /** class of the layer: "leaving" while it fades out */
  className?: string;
  /** the page (line) of the 主題 on screen, for the question box */
  onAt?: (n: number) => void;
  /** the run on screen, for the table of contents */
  onView?: (v: TourView) => void;
}

/** Nothing left to wait for in the run. */
function settled(v: TourView): boolean {
  return !v.creating && v.windows.every((w) => (w.status === "ready" ? w.topics.every((t) => t.state === "done" || t.state === "failed") : w.state === "failed"));
}

/** The 主題 of window `w` to show first: the one covering page `at` (else the one before it), or its first. */
function targetTopic(w: TourWindow, at: number | null): TourTopic | null {
  if (!w.topics.length) return null;
  if (at === null) return w.topics[0];
  return w.topics.find((t) => t.from <= at && at <= t.to) ?? [...w.topics].reverse().find((t) => t.to < at) ?? w.topics[0];
}

/** A 主題's opening words as plain text: the formula markup cut short at either end of the excerpt is left out. */
function plain(text: string): string {
  return text
    .replace(/\$\$?/g, "")
    .replace(/\\(begin|end)\{[^}]*\}/g, "")
    .replace(/\\[a-zA-Z]+\*?(\{([^{}]*)\})?/g, (_m: string, _g: string, arg?: string) => arg ?? "")
    .replace(/[{}]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** The language a guide is set in, read from the guide itself (a book in English may still be guided in Chinese). */
function langOf(g: Guide | null): string | undefined {
  if (!g) return undefined;
  const text = `${g.topic_summary ?? ""}${g.guide ?? ""}${(g.blocks ?? []).map((b) => b.guide).join("")}`.slice(0, 2000);
  return /[\u3400-\u9fff]/.test(text) ? "zh-Hant" : "en";
}

export function GuideTour({ material, session, at, className = "", onAt, onView }: Props) {
  const t = useT();
  const { guideSpan } = usePrefs();
  const mid = material.id;
  const [view, setView] = useState<TourView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [extending, setExtending] = useState<"before" | "after" | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ key: string; top: number } | null>(null);
  // where to bring into view: set when the mode opens or a window is picked, done once its 主題 are laid out
  const target = useRef<{ session: string | null; at: number | null; done: boolean; seg: boolean }>({ session, at, done: false, seg: false });
  const touched = useRef(false);   // the reader has scrolled since: a late layout no longer moves the column
  const onScreen = useRef<{ sid: string; index: number } | null>(null);

  const viewRef = useRef(view);
  viewRef.current = view;
  const report = useRef({ onAt, onView });
  report.current = { onAt, onView };

  /** Keep the 主題 at the reading line where it is while the column above it changes (guides arriving, a window
   *  added before): remember it now, put it back after the render. */
  const capture = () => {
    const el = root.current;
    if (!el || !target.current.done) return;
    const line = el.getBoundingClientRect().top + 96;
    for (const s of el.querySelectorAll<HTMLElement>("[data-key]")) {
      const r = s.getBoundingClientRect();
      if (r.bottom > line) {
        anchor.current = { key: s.dataset.key!, top: r.top };
        return;
      }
    }
  };
  const apply = useCallback((v: TourView) => {
    capture();
    setView(v);
    setError(null);
    report.current.onView?.(v);
  }, []);
  const fail = (e: unknown) => setError((e as RequestError).body?.detail ?? String(e));

  // opened at a window (or another one picked): the work turns towards it
  useEffect(() => {
    target.current = { session, at, done: false, seg: false };
    touched.current = false;
    if (!session) return;
    let live = true;
    api
      .tourFocus(mid, session, null, at)
      .then((v) => live && apply(v))
      .catch((e) => live && fail(e));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mid, session, at]);

  // the server's progress; leaving the mode (or the book) stops the work there
  const found = !!session;
  useEffect(() => {
    if (!found) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      const v = viewRef.current;
      timer = setTimeout(
        () =>
          api
            .tour(mid)
            .then((x) => live && apply(x))
            .catch(() => {})
            .finally(() => live && tick()),
        v && v.active && !settled(v) ? POLL_MS : IDLE_POLL_MS,
      );
    };
    tick();
    const leave = () => {
      try {
        navigator.sendBeacon?.(api.tourPauseUrl(mid)) || api.tourPause(mid).catch(() => {});
      } catch {
        api.tourPause(mid).catch(() => {});
      }
    };
    window.addEventListener("pagehide", leave);
    return () => {
      live = false;
      clearTimeout(timer);
      window.removeEventListener("pagehide", leave);
      api.tourPause(mid).catch(() => {});
    };
  }, [mid, apply, found]);

  // after a render: the column put back under the reader, or brought to the 主題 to show first
  useLayoutEffect(() => {
    const el = root.current;
    if (!el || !view) return;
    const tg = target.current;
    const w = view.windows.find((x) => x.session_id === tg.session);
    if (!tg.done || (!touched.current && tg.seg && w?.status === "ready")) {
      // a window still being segmented is shown by its placeholder; once its 主題 are in, the 主題 itself
      const tp = w ? targetTopic(w, tg.at) : null;
      const node = tp ? document.getElementById(topicAnchor(w!.session_id, tp.index)) : w ? el.querySelector(`[data-key="${w.session_id}:seg"]`) : null;
      if (node) {
        el.scrollTop += node.getBoundingClientRect().top - el.getBoundingClientRect().top - 24;
        target.current = { ...tg, done: true, seg: !tp };
      }
      anchor.current = null;
      return;
    }
    const a = anchor.current;
    anchor.current = null;
    if (!a) return;
    const node = el.querySelector<HTMLElement>(`[data-key="${a.key}"]`);
    if (node) el.scrollTop += node.getBoundingClientRect().top - a.top;
  }, [view]);

  // the 主題 on screen: the question box asks about its page, the work turns towards it once the scrolling settles
  const settle = useRef<ReturnType<typeof setTimeout>>(undefined);
  const track = () => {
    const el = root.current;
    if (!el) return;
    const line = el.getBoundingClientRect().top + Math.min(el.clientHeight / 3, 160);
    let hit: HTMLElement | null = null;
    for (const s of el.querySelectorAll<HTMLElement>("[data-topic]")) {
      if (s.getBoundingClientRect().top <= line) hit = s;
      else break;
    }
    hit ??= el.querySelector<HTMLElement>("[data-topic]");
    if (!hit) return;
    const sid = hit.dataset.sid!;
    const index = Number(hit.dataset.topic);
    const from = Number(hit.dataset.at);
    report.current.onAt?.(from);
    if (onScreen.current?.sid === sid && onScreen.current.index === index) return;
    onScreen.current = { sid, index };
    clearTimeout(settle.current);
    settle.current = setTimeout(() => {
      api
        .tourFocus(mid, sid, index, from)
        .then(apply)
        .catch(() => {});
    }, FOCUS_SETTLE_MS);
  };
  useEffect(() => () => clearTimeout(settle.current), []);

  // past the first (last) 主題: the pages before (after) the run
  const extend = (direction: "before" | "after") => {
    const v = viewRef.current;
    if (!v || extending || !touched.current) return;
    const edge = direction === "before" ? v.windows[0] : v.windows[v.windows.length - 1];
    if (!edge || edge.status !== "ready" || (direction === "before" ? v.first : v.last)) return;
    setExtending(direction);
    api
      .tourExtend(mid, edge.session_id, direction, guideSpan)
      .then(apply)
      .catch(fail)
      .finally(() => setExtending(null));
  };
  const onScroll = () => {
    const el = root.current;
    if (!el) return;
    track();
    if (el.scrollTop < EDGE_PX) extend("before");
    else if (el.scrollHeight - el.scrollTop - el.clientHeight < EDGE_PX) extend("after");
  };
  // a column too short to scroll: the wheel past its edge asks for more just the same
  const onWheel = (e: React.WheelEvent) => {
    touched.current = true;
    const el = root.current;
    if (!el) return;
    if (e.deltaY < 0 && el.scrollTop <= 0) extend("before");
    if (e.deltaY > 0 && el.scrollHeight - el.scrollTop - el.clientHeight <= 1) extend("after");
  };

  const retry = (sid: string, index: number | null) =>
    api
      .tourRetry(mid, sid, index)
      .then(apply)
      .catch(fail);

  const pageOf = (from: number, to: number) =>
    material.unit === "page"
      ? from === to
        ? t("material.unit.pageNo", { n: from })
        : t("material.range.span", { start: from, end: to, unit: t("material.unit.page", { count: 2 }) })
      : t("reader.atLine", { n: from });

  return (
    <div
      ref={root}
      className={`reader-body reader-mode tour-layer ${className}`}
      aria-hidden={className ? true : undefined}
      onScroll={onScroll}
      onWheel={onWheel}
      onTouchMove={() => (touched.current = true)}
      onKeyDown={() => (touched.current = true)}
    >
      <div className="tour-col">
        {session && view && view.windows.length > 0 && (
          <Edge side="before" end={view.first} busy={extending === "before"} />
        )}
        {error && <div className="tour-error" role="alert">{error}</div>}
        {(!session || (!view && !error)) && (
          <article className="tour-sheet tour-pending is-working tour-wait">
            {at !== null && <p className="tour-where">{pageOf(Math.max(1, at - guideSpan), Math.min(material.units.length || at, at + guideSpan))}</p>}
            <p className="tour-status">{t("tour.opening")}</p>
            <Shimmer />
          </article>
        )}
        {session && view?.windows.map((w) => (
          <section key={w.session_id} className="tour-window" aria-label={pageOf(w.start, w.end)}>
            {w.status === "segmenting" ? (
              <article className={`tour-sheet tour-pending is-${w.state}`} data-key={`${w.session_id}:seg`}>
                <p className="tour-where">{pageOf(w.start, w.end)}</p>
                {w.state === "failed" ? (
                  <Failed text={w.error?.detail ?? t("tour.failed")} onRetry={() => retry(w.session_id, null)} />
                ) : (
                  <>
                    <p className="tour-status">{t(w.state === "working" ? "tour.segmenting" : "tour.segmentQueued")}</p>
                    <Shimmer />
                  </>
                )}
              </article>
            ) : (
              <>
                {w.skipped.length > 0 && (
                  <details className="tour-skipped">
                    <summary>{t("tour.skipped", { n: w.skipped.length })}</summary>
                    {w.skipped.map((x, i) => (
                      <p key={i}>
                        <span>{x.label}</span> {x.text.length > 160 ? `${x.text.slice(0, 160)}…` : x.text}
                      </p>
                    ))}
                  </details>
                )}
                {w.topics.map((tp) => (
                  <Topic key={tp.index} sid={w.session_id} topic={tp} where={pageOf(tp.from, tp.to)} onRetry={() => retry(w.session_id, tp.index)} />
                ))}
              </>
            )}
          </section>
        ))}
        {session && view?.creating && (
          <article className="tour-sheet tour-pending working">
            <p className="tour-where">{pageOf(view.creating.start, view.creating.end)}</p>
            <p className="tour-status">{t("tour.opening")}</p>
            <Shimmer />
          </article>
        )}
        {session && view && view.windows.length > 0 && <Edge side="after" end={view.last} busy={extending === "after"} />}
      </div>
    </div>
  );
}

function Edge({ side, end, busy }: { side: "before" | "after"; end: boolean; busy: boolean }) {
  const t = useT();
  const text = end ? t(side === "before" ? "tour.start" : "tour.end") : busy ? t(side === "before" ? "tour.loading.before" : "tour.loading.after") : t(side === "before" ? "tour.more.before" : "tour.more.after");
  return (
    <p className={`tour-edge ${side} ${busy ? "busy" : ""} ${end ? "end" : ""}`}>
      <span>{text}</span>
    </p>
  );
}

function Shimmer() {
  return (
    <div className="tour-shimmer" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  );
}

function Failed({ text, onRetry }: { text: string; onRetry: () => void }) {
  const t = useT();
  return (
    <div className="tour-failed" role="alert">
      <p>{text}</p>
      <button type="button" onClick={onRetry}>
        {t("tour.retry")}
      </button>
    </div>
  );
}

/** One 主題: where it is, its heading and what it is about, then its 區塊 — the source (folded to a few lines) and
 *  the guide — and the question to check oneself with. A 主題 not written yet shows its opening words. */
function Topic({ sid, topic, where, onRetry }: { sid: string; topic: TourTopic; where: string; onRetry: () => void }) {
  const t = useT();
  const g = topic.guide;
  return (
    <article
      id={topicAnchor(sid, topic.index)}
      className={`tour-sheet tour-topic ${g ? "written" : `tour-pending is-${topic.state}`}`}
      lang={langOf(g)}
      data-key={`${sid}:${topic.index}`}
      data-topic={topic.index}
      data-sid={sid}
      data-at={topic.from}
    >
      <p className="tour-where">{where}</p>
      {topic.heading && <h2 className="tour-heading">{topic.heading}</h2>}
      {g ? (
        <GuideBody guide={g} />
      ) : (
        <>
          {topic.excerpt && plain(topic.excerpt) && <p className="tour-excerpt">{plain(topic.excerpt)}</p>}
          {topic.state === "failed" ? (
            <Failed text={topic.error?.detail ? `${t("tour.failed")}：${topic.error.detail}` : t("tour.failed")} onRetry={onRetry} />
          ) : (
            <>
              <p className="tour-status">{t(topic.state === "working" ? "tour.working" : "tour.queued")}</p>
              <Shimmer />
            </>
          )}
        </>
      )}
    </article>
  );
}

function GuideBody({ guide }: { guide: Guide }) {
  const t = useT();
  const blocks = guide.blocks ?? [];
  return (
    <div className="tour-guide">
      {guide.topic_summary && <p className="tour-lead">{guide.topic_summary}</p>}
      {blocks.length > 0 ? (
        blocks.map((b) => (
          <section key={b.index} className="tour-block">
            {b.heading && <h3 className="tour-subheading">{b.heading}</h3>}
            <Source text={b.source} translation={b.chinese_text} />
            {b.status === "accepted" ? (
              <div className="tour-text">
                <RichText text={b.guide} />
                <Notes notes={b.unverified} />
              </div>
            ) : (
              <p className="tour-block-failed">{t("reading.block.failed")}</p>
            )}
          </section>
        ))
      ) : (
        <div className="tour-text">
          <RichText text={guide.guide} />
          <Notes notes={guide.unverified} />
        </div>
      )}
      {guide.check && (
        <aside className="tour-check">
          <span className="tour-check-label">{t("reading.check")}</span>
          <RichText text={guide.check} inline />
        </aside>
      )}
    </div>
  );
}

/** The passage a 區塊 explains: a few lines, unfolded on request. */
function Source({ text, translation }: { text: string; translation?: string | null }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const long = text.length > 220 || text.split("\n").length > 4;
  return (
    <figure className={`tour-source ${open || !long ? "open" : ""}`}>
      <figcaption>{t("tour.source")}</figcaption>
      <div className="tour-source-text">
        <RichText text={text} />
        {translation && (
          <div className="tour-source-zh">
            <RichText text={translation} />
          </div>
        )}
      </div>
      {long && (
        <button type="button" className="tour-source-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {t(open ? "tour.source.less" : "tour.source.more")}
        </button>
      )}
    </figure>
  );
}

function Notes({ notes }: { notes?: GuideNote[] }) {
  const t = useT();
  if (!notes?.length) return null;
  return (
    <details className="tour-notes">
      <summary>{t("reading.unverified.summary", { n: notes.length })}</summary>
      <ul>
        {notes.map((u, i) => (
          <li key={i}>{u.kind === "note" ? u.text : t("reading.unverified.coverage", { source: u.source })}</li>
        ))}
      </ul>
    </details>
  );
}
