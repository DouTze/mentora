// 閱讀器: an imported material opened like a book in the app — PDF pages laid out as paper, Markdown / text set as an
// article — with one box at the foot to ask about the page being read, or to go into 導讀 / 出題 from there.
//
// The companion reads the whole material once in the background (導師全文學習, the normal learning flow over every
// page); its reading parts tell which part the reader is on, so 導讀 starts at that part and a question is answered
// from the units on that page.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, RequestError } from "../api";
import type { Bookmark, Library, MaterialDetail, PdfInfo, PdfOutlineItem, PdfPageLayer, ReaderQuestion, StudyView } from "../types";
import type { Route } from "../route";
import { Icon } from "../components/Icons";
import { PdfLayer } from "../components/PdfLayer";
import { RichText } from "../components/RichText";
import { Select } from "../components/Select";
import { Working } from "../components/Working";
import { useT } from "../i18n";

interface Props {
  id: string;
  library: Library;
  onLibraryChange: () => Promise<unknown>;
  onNavigate: (r: Route) => void;
  onOpenSession: (id: string, opts?: { quiz?: boolean }) => void;
  /** this material's bookmarks (App keeps every book's, the sidebar lists them) */
  bookmarks?: Bookmark[];
  onBookmarks?: (id: string, marks: Bookmark[]) => void;
  /** a page to bring into view, asked for from outside (a bookmark in the sidebar); `k` makes each ask new */
  jump?: { id: string; n: number; k: number } | null;
  /** the page (line) being read, as it changes */
  onPosition?: (id: string, n: number) => void;
}

const STUDY_POLL_MS = 3000;
const EXTRACT_POLL_MS = 2000;
const NO_MARKS: Bookmark[] = [];
export const POSITION_KEY = (id: string) => `mentor.reader.at.${id}`;

function storedAt(id: string): number {
  try {
    return Number(localStorage.getItem(POSITION_KEY(id))) || 1;
  } catch {
    return 1;
  }
}

/** The page (line) whose box crosses the reading line, near the top of the viewport. Listens in the capture
 * phase: the scrolling element is the window in a browser and the shell in the desktop app. */
function usePosition(root: React.RefObject<HTMLElement | null>, onAt: (n: number) => void, deps: unknown[]) {
  useEffect(() => {
    let frame = 0;
    const measure = () => {
      frame = 0;
      const els = root.current?.querySelectorAll<HTMLElement>("[data-at]");
      if (!els?.length || !els[0].getBoundingClientRect().height) return;   // not laid out (yet)
      // just under the top bar: at the top of the document that is the first page (line)
      const line = Math.min(window.innerHeight / 3, 140);
      let lo = 0;
      let hi = els.length - 1;
      // the last element whose top is above the reading line (elements are in document order)
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (els[mid].getBoundingClientRect().top <= line) lo = mid;
        else hi = mid - 1;
      }
      onAt(Number(els[lo].dataset.at));
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(measure);
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", onScroll);
    measure();
    return () => {
      document.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("resize", onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** Open at the page (line) the reader left. */
function useRestore(root: React.RefObject<HTMLElement | null>, at: number, ready: boolean) {
  const done = useRef(false);
  useLayoutEffect(() => {
    if (!ready || done.current) return;
    done.current = true;
    if (at > 1) root.current?.querySelector(`[data-at="${at}"]`)?.scrollIntoView({ block: "start" });
  }, [ready]);
}

// -- PDF ------------------------------------------------------------------------------------
// page layers (text runs, links, images) once per page and material while the app runs; the server caches them too
const layers = new Map<string, Promise<PdfPageLayer | null>>();
function pageLayer(id: string, n: number): Promise<PdfPageLayer | null> {
  const key = `${id}:${n}`;
  if (!layers.has(key)) layers.set(key, api.materialPageLayer(id, n).catch(() => (layers.delete(key), null)));
  return layers.get(key)!;
}

type Goto = (page: number, y?: number) => void;

function PdfPage({ id, n, size, width, onGoto }: { id: string; n: number; size: [number, number]; width: number; onGoto: Goto }) {
  const ref = useRef<HTMLElement>(null);
  const [near, setNear] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [layer, setLayer] = useState<PdfPageLayer | null>(null);
  // the text layer lives only while the page is within about a screen of the view: far pages keep their image only
  const [live, setLive] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return setLive(true);
    const io = new IntersectionObserver((es) => setLive(es.some((e) => e.isIntersecting)), { rootMargin: "100% 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  // stable for the memoised layer: a zoom re-renders the page, not its text runs
  const gotoNow = useRef(onGoto);
  gotoNow.current = onGoto;
  const goto = useCallback<Goto>((p, y) => gotoNow.current(p, y), []);
  const imageUrl = useCallback((x: number) => api.materialImageUrl(id, x), [id]);
  useEffect(() => {
    if (!near) return;
    let live = true;
    pageLayer(id, n).then((l) => live && setLayer(l));
    return () => {
      live = false;
    };
  }, [near, id, n]);
  useEffect(() => {
    const el = ref.current;
    if (!el || near) return;
    if (typeof IntersectionObserver === "undefined") return setNear(true);
    const io = new IntersectionObserver((es) => es.some((e) => e.isIntersecting) && setNear(true), { rootMargin: "1600px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [near]);
  // the server renders a few widths only: ask for the next one up, so a pinch does not reload the page at every step
  const want = width * Math.min(window.devicePixelRatio || 1, 2);
  const px = RENDER_WIDTHS.find((x) => x >= want) ?? RENDER_WIDTHS[RENDER_WIDTHS.length - 1];
  return (
    <figure className="paper" ref={ref} data-at={n} style={{ aspectRatio: `${size[0]} / ${size[1]}`, ["--k" as string]: width / size[0] }}>
      {near && width > 0 && (
        <img src={api.materialPageUrl(id, n, px)} alt="" draggable={false} decoding="async" className={loaded ? "on" : ""} onLoad={() => setLoaded(true)} />
      )}
      {live && width > 0 && layer && <PdfLayer layer={layer} imageUrl={imageUrl} onGoto={goto} />}
    </figure>
  );
}

/** How the pages are sized: the whole page in the window, the column's width, or a zoom of the default width. */
export type Zoom = { fit: "page" | "width" } | { scale: number };
export const ZOOM_STEPS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const BASE_WIDTH = 820;          // a page at 100%
const RENDER_WIDTHS = [480, 720, 960, 1280, 1600, 2000];   // MaterialStore.PAGE_WIDTHS
export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 3;

/** The element that scrolls the reader: the window in a browser, the shell in the desktop app. */
function scrollParent(el: HTMLElement): HTMLElement {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight) return p;
  }
  return (document.scrollingElement as HTMLElement) ?? document.documentElement;
}
const CHROME_H = 64 + 150;       // top bar + question box, kept clear in 整頁

function pageWidth(zoom: Zoom, size: [number, number], avail: number): number {
  if ("scale" in zoom) return Math.round(BASE_WIDTH * zoom.scale);
  if (zoom.fit === "width") return Math.max(240, avail);
  return Math.max(200, Math.min(avail, Math.round(((window.innerHeight - CHROME_H) * size[0]) / size[1])));
}

function PdfReader({ material, at, zoom, focus, gotoRef, onAt, onScale }: {
  material: MaterialDetail;
  /** set to a function that brings a page (and a point on it) to the top: links, the outline */
  gotoRef: React.RefObject<Goto | null>;
  at: number;
  zoom: Zoom;
  /** where a pinch / ctrl-wheel happened (client coordinates); that point stays put while the pages grow */
  focus: React.RefObject<{ x: number; y: number } | null>;
  onAt: (n: number) => void;
  onScale: (s: number) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const [sizes, setSizes] = useState<[number, number][] | null>(null);
  const [avail, setAvail] = useState(0);
  useEffect(() => {
    api
      .materialPages(material.id)
      .then((r) => setSizes(r.sizes))
      .catch(() => setSizes([]));
  }, [material.id]);
  // the room the reader body gives the pages (its width less its padding)
  useLayoutEffect(() => {
    const body = root.current?.parentElement;
    if (!body) return;
    const measure = () => {
      const cs = getComputedStyle(body);
      setAvail(body.clientWidth - parseFloat(cs.paddingLeft || "0") - parseFloat(cs.paddingRight || "0"));
    };
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(body);
    window.addEventListener("resize", measure);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [sizes]);
  // pages the server could not measure still get a page shape (A4)
  const pages = sizes?.length ? sizes : Array.from({ length: material.unit_count }, () => [595, 842] as [number, number]);
  const width = avail > 0 ? pageWidth(zoom, pages[Math.max(0, Math.min(at, pages.length) - 1)], avail) : 0;
  // the percentage shown for a fitted zoom
  useEffect(() => {
    if (width) onScale(width / BASE_WIDTH);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width]);
  // a new zoom keeps the reader where they were: 整頁 shows the current page whole; any other zoom scales the scroll
  // position around the focus (the pinch point, else the reading line) so the text under it stays under it
  const keep = useRef(at);
  keep.current = at;
  const prev = useRef(0);
  // while the size changes (a pinch, ctrl-wheel, the zoom buttons) the text layers are hidden, so each step only
  // scales the page images; they come back once the size has settled
  const settle = useRef(0);
  useLayoutEffect(() => {
    const el = root.current;
    if (!el || !prev.current || prev.current === width) return;
    el.classList.add("zooming");
    clearTimeout(settle.current);
    settle.current = window.setTimeout(() => el.classList.remove("zooming"), 220);
  }, [width]);
  useEffect(() => () => clearTimeout(settle.current), []);
  useLayoutEffect(() => {
    const el = root.current;
    const old = prev.current;
    prev.current = width;
    if (!el || !width || !old || old === width) return;
    if ("fit" in zoom && zoom.fit === "page" && !focus.current) {
      el.querySelector(`[data-at="${keep.current}"]`)?.scrollIntoView({ block: "start" });
      return;
    }
    const sc = scrollParent(el);
    const top = sc === document.scrollingElement ? 0 : sc.getBoundingClientRect().top;
    const off = el.getBoundingClientRect().top - top + sc.scrollTop;   // where the pages start in the scrolled content
    const fy = (focus.current?.y ?? Math.min(window.innerHeight / 3, 140)) - top;
    const r = width / old;
    sc.scrollTop = (sc.scrollTop + fy - off) * r + off - fy;
    const body = el.parentElement;
    if (body && body.scrollWidth > body.clientWidth) {
      const fx = (focus.current?.x ?? body.getBoundingClientRect().left + body.clientWidth / 2) - body.getBoundingClientRect().left;
      body.scrollLeft = (body.scrollLeft + fx) * r - fx;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width]);
  usePosition(root, onAt, [sizes, width]);
  useRestore(root, at, !!sizes?.length && width > 0);
  const goto = useCallback<Goto>(
    (page, y) => {
      const el = root.current?.querySelector<HTMLElement>(`[data-at="${page}"]`);
      if (!el) return;
      el.scrollIntoView({ block: "start" });
      const size = pages[page - 1];
      // a point on the page: scroll on to it, keeping a little of what comes before in view
      if (y && size && width) scrollParent(el).scrollTop += Math.max(0, (y * width) / size[0] - 24);
    },
    [pages, width],
  );
  gotoRef.current = goto;
  return (
    <div className="pdf-pages" ref={root} style={{ width: width || undefined }}>
      {sizes !== null && pages.map((s, i) => <PdfPage key={i} id={material.id} n={i + 1} size={s} width={width} onGoto={goto} />)}
    </div>
  );
}

// -- Markdown / text --------------------------------------------------------------------------
/** Paragraph blocks with the line each starts on; a fenced code block stays one block across blank lines. */
function blocksOf(lines: string[]): { line: number; text: string }[] {
  const out: { line: number; text: string }[] = [];
  let cur: { line: number; rows: string[] } | null = null;
  let fence = false;
  lines.forEach((ln, i) => {
    if (/^\s*(```|~~~)/.test(ln)) fence = !fence;
    if (!ln.trim() && !fence) {
      if (cur) out.push({ line: cur.line, text: cur.rows.join("\n") });
      cur = null;
      return;
    }
    if (!cur) cur = { line: i + 1, rows: [] };
    cur.rows.push(ln);
  });
  if (cur) out.push({ line: (cur as { line: number }).line, text: (cur as { rows: string[] }).rows.join("\n") });
  return out;
}

function TextReader({ material, at, scale, onAt }: { material: MaterialDetail; at: number; scale: number; onAt: (n: number) => void }) {
  const root = useRef<HTMLElement>(null);
  const blocks = useMemo(() => blocksOf(material.units), [material.units]);
  usePosition(root, onAt, [blocks]);
  useRestore(root, at, blocks.length > 0);
  // the restored line may sit inside a block: scroll to the block that holds it
  useLayoutEffect(() => {
    if (at <= 1 || root.current?.querySelector(`[data-at="${at}"]`)) return;
    const holder = [...blocks].reverse().find((b) => b.line <= at);
    if (holder) root.current?.querySelector(`[data-at="${holder.line}"]`)?.scrollIntoView({ block: "start" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blocks]);
  return (
    <article className={`prose-reader ${material.kind}`} ref={root} style={{ "--zoom": scale } as React.CSSProperties}>
      {blocks.map((b) =>
        material.kind === "md" ? (
          <div key={b.line} data-at={b.line}>
            {/^\s*(```|~~~)/.test(b.text) ? <pre><code>{b.text.replace(/^\s*(```|~~~).*\n?|\n?\s*(```|~~~)\s*$/g, "")}</code></pre> : <RichText text={b.text} />}
          </div>
        ) : (
          <p key={b.line} data-at={b.line}>
            {b.text}
          </p>
        ),
      )}
    </article>
  );
}

// -- the companion's study --------------------------------------------------------------------
const BEAT_MS = 20_000;

/** The companion reads along: while this book is open (and the window visible) the page tells the server so — the
 * study runs; leaving, hiding the window or opening another book pauses it. */
function useStudy(id: string): [StudyView | null, (action: "pause" | "resume" | "cancel") => Promise<void>] {
  const [study, setStudy] = useState<StudyView | null>(null);
  useEffect(() => {
    setStudy(null);
    let alive = true;
    let poll: ReturnType<typeof setTimeout> | undefined;
    const show = (v: StudyView) => {
      if (!alive) return;
      setStudy(v);
      clearTimeout(poll);
      if (v.status === "reading" && !v.paused) poll = setTimeout(load, STUDY_POLL_MS);
    };
    const load = () =>
      api
        .study(id)
        .then(show)
        .catch(() => alive && (poll = setTimeout(load, STUDY_POLL_MS * 4)));
    const focus = () =>
      document.visibilityState !== "hidden" &&
      api
        .studyAction(id, "focus")
        .then(show)
        .catch(() => {});
    const leave = () => {
      try {
        navigator.sendBeacon?.(api.studyLeaveUrl(id)) || api.studyAction(id, "leave").catch(() => {});
      } catch {
        api.studyAction(id, "leave").catch(() => {});
      }
    };
    const onVisible = () => (document.visibilityState === "hidden" ? leave() : focus());
    focus();
    const beat = setInterval(focus, BEAT_MS);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pagehide", leave);
    return () => {
      alive = false;
      clearTimeout(poll);
      clearInterval(beat);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pagehide", leave);
      api.studyAction(id, "leave").catch(() => {});
    };
  }, [id]);
  const act = useCallback(
    async (action: "pause" | "resume" | "cancel") => {
      const v = await api.studyAction(id, action);
      setStudy(v);
      if (v.status === "reading" && !v.paused) setTimeout(() => api.study(id).then(setStudy).catch(() => {}), STUDY_POLL_MS);
    },
    [id],
  );
  return [study, act];
}

function StudyBadge({ study, onAction }: { study: StudyView | null; onAction: (a: "pause" | "resume" | "cancel") => Promise<void> }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  if (!study) return null;
  const pct = Math.round(study.progress * 100);
  const paused = study.status === "reading" && !!study.paused;
  const label =
    study.status === "ready" ? t("reader.study.ready")
    : study.status === "failed" ? t("reader.study.failed")
    : study.status === "off" ? t("reader.study.off")
    : paused ? t(study.by_user ? "reader.study.pausedByYou" : "reader.study.paused", { pct })
    : pct > 0 ? t("reader.study.progress", { pct })
    : t("reader.study.reading");
  const run = async (a: "pause" | "resume" | "cancel") => {
    setBusy(true);
    setConfirmCancel(false);
    try {
      await onAction(a);
    } finally {
      setBusy(false);
    }
  };
  const step = study.step;
  const count = step?.current && step.total ? t(step.approx ? "reader.study.countApprox" : "reader.study.count", { current: step.current, total: step.total }) : step?.current ? t("reader.study.countOpen", { current: step.current }) : "";
  // the two passes of the planner, in order; the one running is lit, finished ones are ticked
  const phases: { key: string; label: string; state: "done" | "now" | "todo" }[] =
    step?.phase === "window"
      ? [{ key: "window", label: t("reader.study.phase.window"), state: "now" }]
      : [
          { key: "extract", label: t("reader.study.phase.extract"), state: step?.phase === "extract" ? "now" : "done" },
          { key: "outline", label: t("reader.study.phase.outline"), state: step?.phase === "outline" ? "now" : step?.phase === "extract" ? "todo" : "done" },
          { key: "topic", label: t("reader.study.phase.topic"), state: step?.phase === "topic" ? "now" : "todo" },
        ];
  return (
    <span
      className={`reader-study ${study.status} ${paused ? "paused" : ""}`}
      role="status"
      tabIndex={0}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => {
        setOpen(false);
        setConfirmCancel(false);
      }}
      onFocus={() => setOpen(true)}
      onBlur={(e) => !e.currentTarget.contains(e.relatedTarget as Node) && setOpen(false)}
    >
      <span className="reader-study-light" aria-hidden="true" />
      {label}
      {open && (
        <span className="reader-study-card" role="tooltip">
          <span className="reader-study-head">{label}</span>
          {study.status === "reading" && step && !paused && (
            <span className="reader-study-steps">
              {phases.map((p) => (
                <span key={p.key} className={`reader-study-step ${p.state}`}>
                  <span className="dot" aria-hidden="true" />
                  <span className="grow">{p.label}</span>
                  {p.state === "now" && p.key !== "extract" && count && <span className="n">{count}</span>}
                </span>
              ))}
            </span>
          )}
          {study.status === "reading" && (
            <span className="reader-study-bar" aria-hidden="true">
              <span style={{ width: `${Math.max(pct, 2)}%` }} />
            </span>
          )}
          {study.status === "ready" && <span className="reader-study-note">{t("reader.study.parts", { count: study.parts.length })}</span>}
          {study.status === "failed" && study.error && <span className="reader-study-note bad">{study.error}</span>}
          {paused && !study.by_user && <span className="reader-study-note">{t("reader.study.heldNote")}</span>}
          <span className="reader-study-note">{t("reader.study.hint")}</span>
          {study.status !== "ready" && (
            <span className="reader-study-actions">
              {study.status === "off" ? (
                <button type="button" onClick={() => run("resume")} disabled={busy}>
                  {t("reader.study.start")}
                </button>
              ) : confirmCancel ? (
                <>
                  <span className="grow">{t("reader.study.cancelConfirmText")}</span>
                  <button type="button" className="danger" onClick={() => run("cancel")} disabled={busy}>
                    {t("reader.study.cancelConfirm")}
                  </button>
                  <button type="button" onClick={() => setConfirmCancel(false)}>
                    {t("common.cancel")}
                  </button>
                </>
              ) : (
                <>
                  {paused && study.by_user ? (
                    <button type="button" onClick={() => run("resume")} disabled={busy}>
                      {t("reader.study.resume")}
                    </button>
                  ) : study.status === "reading" ? (
                    <button type="button" onClick={() => run("pause")} disabled={busy}>
                      {t("reader.study.pause")}
                    </button>
                  ) : (
                    <button type="button" onClick={() => run("resume")} disabled={busy}>
                      {t("reader.study.retry")}
                    </button>
                  )}
                  <button type="button" className="quiet" onClick={() => setConfirmCancel(true)} disabled={busy}>
                    {t("reader.study.cancel")}
                  </button>
                </>
              )}
            </span>
          )}
        </span>
      )}
    </span>
  );
}

// -- the question box ---------------------------------------------------------------------------
function Composer({ material, at, unitWord, onOpenSession }: { material: MaterialDetail; at: number; unitWord: string; onOpenSession: Props["onOpenSession"] }) {
  const t = useT();
  const [text, setText] = useState("");
  const [qa, setQa] = useState<ReaderQuestion[]>([]);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [going, setGoing] = useState<"guide" | "quiz" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const thread = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setQa([]);
    setOpen(false);
    api
      .readerQuestions(material.id)
      .then(setQa)
      .catch(() => {});
  }, [material.id]);

  // the box grows with what is typed, up to a few lines
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text]);

  useEffect(() => {
    if (open) thread.current?.scrollTo({ top: thread.current.scrollHeight, behavior: "smooth" });
  }, [open, qa.length, pending]);

  // the text of a book just imported is still being read (or could not be): asking and 導讀 / 出題 wait for it
  const preparing = material.status === "extracting";
  const unreadable = material.status === "failed";
  const blocked = preparing || unreadable;

  const send = async () => {
    const q = text.trim();
    if (!q || pending || blocked) return;
    setError(null);
    setPending(q);
    setOpen(true);
    setText("");
    try {
      const rec = await api.readerAsk(material.id, at, q);
      setQa((xs) => [...xs, rec]);
    } catch (e) {
      setText(q);   // the question stays and can be sent again
      setError((e as RequestError).body?.detail ?? String(e));
    } finally {
      setPending(null);
    }
  };

  const learn = async (kind: "guide" | "quiz") => {
    setError(null);
    setGoing(kind);
    try {
      const s = await api.readerLearn(material.id, at);
      onOpenSession(s.id, { quiz: kind === "quiz" });
    } catch (e) {
      setError((e as RequestError).body?.detail ?? String(e));
      setGoing(null);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    } else if (e.key === "Escape" && open) {
      setOpen(false);
    }
  };

  const where = (n: number) => (material.unit === "page" ? t("material.unit.pageNo", { n }) : t("reader.atLine", { n }));

  return (
    <div className="reader-dock">
      <div className="reader-dock-inner">
      {open && (qa.length > 0 || pending) && (
        <section className="reader-thread" aria-label={t("reader.thread")}>
          <header>
            <span>{t("reader.thread")}</span>
            <button type="button" className="icon-btn" aria-label={t("common.close")} onClick={() => setOpen(false)}>
              <Icon.close />
            </button>
          </header>
          <div className="reader-thread-body" ref={thread}>
            {qa.map((x) => (
              <article key={x.id} className="reader-qa">
                <p className="reader-q">{x.question}</p>
                <RichText className="reader-a" text={x.answer} />
                <p className="reader-qa-meta">
                  {where(x.unit_no)}
                  {x.used_guide && <> · {t("reader.usedGuide")}</>}
                </p>
              </article>
            ))}
            {pending && (
              <article className="reader-qa">
                <p className="reader-q">{pending}</p>
                <Working label={t("reader.thinking")} />
              </article>
            )}
          </div>
        </section>
      )}
      <form
        className="reader-ask"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <textarea
          ref={input}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          placeholder={preparing ? t("reader.preparing") : unreadable ? t("reader.unreadable") : t(material.unit === "page" ? "reader.placeholder" : "reader.placeholder.line")}
          disabled={blocked}
          title={unreadable && material.status_detail ? material.status_detail : undefined}
          aria-label={t("reader.ask")}
        />
        {error && (
          <p className="reader-error" role="alert">
            {error}
          </p>
        )}
        <div className="reader-ask-bar">
          <span className="reader-at">{where(at)}</span>
          {qa.length > 0 && !open && (
            <button type="button" className="reader-chip" onClick={() => setOpen(true)}>
              {t("reader.threadCount", { count: qa.length })}
            </button>
          )}
          <span className="spacer" />
          <button type="button" className="reader-action" onClick={() => learn("guide")} disabled={!!going || blocked} title={t("reader.guide.hint", { unit: unitWord })}>
            <Icon.book />
            {going === "guide" ? t("reader.going") : t("reader.guide")}
          </button>
          <button type="button" className="reader-action" onClick={() => learn("quiz")} disabled={!!going || blocked} title={t("reader.quiz.hint", { unit: unitWord })}>
            <Icon.quiz />
            {going === "quiz" ? t("reader.going") : t("reader.quiz")}
          </button>
          <button type="submit" className="reader-send" disabled={!text.trim() || !!pending || blocked} aria-label={t("reader.send")}>
            <Icon.arrowUp />
          </button>
        </div>
      </form>
      </div>
    </div>
  );
}

// -- outline and document info -------------------------------------------------------------------------
type OutlineRow = { item: PdfOutlineItem; depth: number; num: string };
/** A heading whose title already carries its number ("3.2 …", "Chapter 4", "第三章", "IV. …") keeps it; the others
 * are numbered by their place in the outline (1, 1.1, 1.1.2 …). */
const NUMBERED = /^\s*(\d+(\.\d+)*\.?\s|[IVXLC]+\.\s|[A-Z]\.\s|(chapter|part|section|appendix)\b|第\s*[\d一二三四五六七八九十百]+\s*[章節部篇卷])/i;
function flatten(items: PdfOutlineItem[], depth = 0, out: OutlineRow[] = [], prefix = ""): OutlineRow[] {
  items.forEach((item, i) => {
    const num = `${prefix}${i + 1}`;
    out.push({ item, depth, num: NUMBERED.test(item.title) ? "" : num });
    flatten(item.children, depth + 1, out, `${num}.`);
  });
  return out;
}
export const outlineRows = (items: PdfOutlineItem[]) => flatten(items);

/** The chapter a page falls in: the last outline heading (in reading order) at or before it, with its number — the
 * one the table of contents marks as being read. */
export function chapterAt(rows: OutlineRow[], page: number): string | null {
  let hit: OutlineRow | null = null;
  for (const r of rows) if (r.item.page && r.item.page <= page) hit = r;
  return hit ? `${hit.num ? `${hit.num} ` : ""}${hit.item.title}`.trim() : null;
}

/** The Markdown heading a line falls under. */
function headingAt(lines: string[], line: number): string | null {
  for (let i = Math.min(line, lines.length) - 1; i >= 0; i--) {
    const m = /^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/.exec(lines[i]);
    if (m) return m[1];
  }
  return null;
}

/** The PDF's bookmarks, the section being read marked; picking one brings its page (and point) to the top. */
function OutlineMenu({ info, at, onGoto }: { info: PdfInfo; at: number; onGoto: Goto }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const rows = useMemo(() => flatten(info.outline), [info]);
  let current = -1;
  rows.forEach((r, i) => {
    if (r.item.page && r.item.page <= at) current = i;
  });
  useEffect(() => {
    if (!open) return;
    list.current?.querySelector(".current")?.scrollIntoView({ block: "center" });
    const onDown = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="reader-more" ref={box}>
      <button type="button" className="icon-btn" aria-haspopup="menu" aria-expanded={open} aria-label={t("reader.toc")} title={t("reader.toc")} onClick={() => setOpen((v) => !v)}>
        <Icon.toc />
      </button>
      {open && (
        <div className="menu reader-menu reader-toc" role="menu" ref={list}>
          {rows.length === 0 && <p className="reader-menu-facts">{t("reader.toc.empty")}</p>}
          {rows.map((r, i) => (
            <button
              key={i}
              type="button"
              role="menuitem"
              className={`${i === current ? "current" : ""} ${r.depth === 0 ? "top" : ""}`.trim() || undefined}
              style={{ paddingLeft: 10 + r.depth * 16 }}
              disabled={!r.item.page}
              onClick={() => {
                if (!r.item.page) return;
                setOpen(false);
                onGoto(r.item.page, r.item.y);
              }}
            >
              {r.num && (
                // one width per depth, so the titles of a level start on one line however long the number
                <span className="toc-num" style={{ minWidth: `${(r.depth + 1) * 2}ch` }}>
                  {r.num}
                </span>
              )}
              <span className="toc-title">{r.item.title}</span>
              {r.item.page && <span className="toc-page">{info.labels?.[r.item.page - 1] ?? r.item.page}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** The material's colophon, opened from the reader's ⋯: its cover and title, the category it is filed under, what
 * the file is and holds (only what it has), its attachments, and deleting it. */
function MaterialCard({ material, info, library, zoom, onCategory, onDelete }: {
  material: MaterialDetail;
  info: PdfInfo | null;
  library: Library;
  /** the page size controls, kept here rather than on the toolbar */
  zoom: ReactNode;
  onCategory: (id: string | null) => void;
  onDelete: () => void;
}) {
  const t = useT();
  const [confirm, setConfirm] = useState(false);
  const [cover, setCover] = useState(material.kind === "pdf");
  const n = (v: number) => v.toLocaleString();
  const unit = t(material.unit === "page" ? "material.unit.page" : "material.unit.line", { count: material.unit_count });
  const format = material.kind === "pdf" ? `PDF${info?.pdf_version ? ` ${info.pdf_version}` : ""}` : material.kind === "md" ? "Markdown" : t("reader.info.plain");
  const facts: [string, string, string?][] = [
    [t("reader.info.length"), t("material.unitCount", { count: n(material.unit_count), unit })],
    [t("reader.info.format"), format],
    [t("reader.info.file"), material.library?.original_name ?? material.filename, material.library?.original_name ?? material.filename],
  ];
  const lib = material.library;
  if (lib) {
    facts.push([t("reader.info.source"), lib.original_path ? lib.original_path.replace(/^\/Users\/[^/]+/, "~") : t("reader.info.upload"), lib.original_path ?? undefined]);
    facts.push([t("reader.info.copy"), lib.stored_name, `~/.mentora/${lib.stored_name}\nSHA-256 ${lib.sha256}`]);
  }
  if (info) {
    if (info.counts.links) facts.push([t("reader.info.links"), n(info.counts.links)]);
    if (info.counts.images) facts.push([t("reader.info.images"), n(info.counts.images)]);
    if (info.counts.annotations) facts.push([t("reader.info.annotations"), n(info.counts.annotations)]);
  }
  const byline = info?.author || (info?.title && info.title !== material.title ? info.title : null);
  return (
    <div className="reader-card" role="dialog" aria-label={t("reader.more")}>
      <header className="rc-head">
        <span className={`rc-cover${cover ? "" : " plain"}`} aria-hidden="true">
          {cover ? <img src={api.materialCoverUrl(material.id)} alt="" onError={() => setCover(false)} /> : <span>{material.title.trim().slice(0, 1)}</span>}
        </span>
        <span className="rc-title">
          <strong title={material.title}>{material.title}</strong>
          {byline && <small title={byline}>{byline}</small>}
          {lib?.alias && (
            <span className="rc-alias" title={t("reader.info.alias")}>
              {lib.alias}
            </span>
          )}
        </span>
      </header>
      {lib?.description && (
        <p className="rc-about" title={lib.described_by ? t("reader.info.describedBy", { model: lib.described_by.split(":").slice(1).join(":") || lib.described_by }) : undefined}>
          {lib.description}
        </p>
      )}
      {/* the controls name themselves (−, 100%, +, 整頁, 符合寬度) and take the whole row */}
      <div className="rc-row rc-zoom">{zoom}</div>
      <div className="rc-row rc-category">
        <span>{t("material.category")}</span>
        <Select
          label={t("material.category")}
          value={material.category_id ?? ""}
          onChange={(v) => onCategory(v || null)}
          options={[{ value: "", label: t("common.uncategorized") }, ...library.categories.map((c) => ({ value: c.id, label: c.name }))]}
        />
      </div>
      <dl className="rc-facts">
        {facts.map(([k, v, title]) => (
          <div key={k}>
            <dt>{k}</dt>
            <i aria-hidden="true" />
            <dd title={title}>{v}</dd>
          </div>
        ))}
      </dl>
      {info && info.attachments.length > 0 && (
        <div className="rc-files">
          <span className="rc-label">{t("reader.info.attachments")}</span>
          {info.attachments.map((a, i) => (
            <a key={i} href={api.materialAttachmentUrl(material.id, i)} download={a.name} title={a.description ?? a.name}>
              <Icon.download />
              <span>{a.name}</span>
              {a.size ? <small>{a.size < 1024 ? `${a.size} B` : a.size < 1048576 ? `${Math.round(a.size / 1024)} KB` : `${(a.size / 1048576).toFixed(1)} MB`}</small> : null}
            </a>
          ))}
        </div>
      )}
      <footer className={`rc-foot${confirm ? " confirm" : ""}`}>
        {confirm ? (
          <>
            <p>{t("material.delete.confirmText")}</p>
            <div className="rc-actions">
              <button type="button" className="rc-danger" onClick={onDelete} autoFocus>
                {t("material.delete.confirm")}
              </button>
              <button type="button" className="rc-ghost" onClick={() => setConfirm(false)}>
                {t("common.cancel")}
              </button>
            </div>
          </>
        ) : (
          <button type="button" className="rc-delete" onClick={() => setConfirm(true)}>
            <Icon.trash /> {t("material.delete")}
          </button>
        )}
      </footer>
    </div>
  );
}

// -- zoom ------------------------------------------------------------------------------------------------
function ZoomBar({ zoom, shown, pdf, onZoom }: { zoom: Zoom; shown: number; pdf: boolean; onZoom: (z: Zoom) => void }) {
  const t = useT();
  const step = (dir: 1 | -1) => {
    const i = ZOOM_STEPS.findIndex((x) => x >= shown - 0.001);
    const cur = i < 0 ? ZOOM_STEPS.length - 1 : i;
    // from a fitted size, the first step lands on the nearest listed size in that direction
    const exact = Math.abs(ZOOM_STEPS[cur] - shown) < 0.01;
    const next = dir > 0 ? (exact ? cur + 1 : cur) : cur - 1;
    onZoom({ scale: ZOOM_STEPS[Math.max(0, Math.min(ZOOM_STEPS.length - 1, next))] });
  };
  return (
    <div className="reader-zoom" role="group" aria-label={t("reader.zoom")}>
      <button type="button" onClick={() => step(-1)} disabled={shown <= ZOOM_STEPS[0] + 0.001} aria-label={t("reader.zoom.out")} title={t("reader.zoom.out")}>
        −
      </button>
      <button type="button" className="zoom-pct" onClick={() => onZoom({ scale: 1 })} title={t("reader.zoom.reset")}>
        {Math.round(shown * 100)}%
      </button>
      <button type="button" onClick={() => step(1)} disabled={shown >= ZOOM_STEPS[ZOOM_STEPS.length - 1] - 0.001} aria-label={t("reader.zoom.in")} title={t("reader.zoom.in")}>
        +
      </button>
      {pdf && (
        <>
          <span className="zoom-sep" aria-hidden="true" />
          <button type="button" aria-pressed={"fit" in zoom && zoom.fit === "page"} onClick={() => onZoom({ fit: "page" })}>
            {t("reader.zoom.page")}
          </button>
          <button type="button" aria-pressed={"fit" in zoom && zoom.fit === "width"} onClick={() => onZoom({ fit: "width" })}>
            {t("reader.zoom.width")}
          </button>
        </>
      )}
    </div>
  );
}

const ZOOM_KEY = (kind: string) => `mentor.reader.zoom.${kind === "pdf" ? "pdf" : "text"}`;

function storedZoom(kind: string): Zoom {
  try {
    const z = JSON.parse(localStorage.getItem(ZOOM_KEY(kind)) ?? "null");
    if (z && (("fit" in z && (z.fit === "page" || z.fit === "width")) || ("scale" in z && typeof z.scale === "number"))) return z;
  } catch {}
  return { scale: 1 };
}

// -- the page -----------------------------------------------------------------------------------------
export function MaterialPage({ id, library, onLibraryChange, onNavigate, onOpenSession, bookmarks = NO_MARKS, onBookmarks, jump = null, onPosition }: Props) {
  const t = useT();
  const [material, setMaterial] = useState<MaterialDetail | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);
  const [at, setAt] = useState(() => storedAt(id));
  const [study, studyAction] = useStudy(id);
  const menuBox = useRef<HTMLDivElement>(null);
  const gotoRef = useRef<Goto | null>(null);
  const [info, setInfo] = useState<PdfInfo | null>(null);
  useEffect(() => {
    setInfo(null);
    if (material?.id !== id || material.kind !== "pdf") return;
    let live = true;
    api
      .materialInfo(id)
      .then((i) => live && setInfo(i))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [id, material?.id, material?.kind]);
  const [zoom, setZoomState] = useState<Zoom>({ scale: 1 });
  const [shown, setShown] = useState(1);   // the zoom as a percentage, also when fitted
  const kind = material?.kind ?? "";
  useEffect(() => {
    if (!kind) return;
    const z = storedZoom(kind);
    setZoomState(z);
    if ("scale" in z) setShown(z.scale);
  }, [kind]);
  const setZoom = useCallback(
    (z: Zoom) => {
      setZoomState(z);
      if ("scale" in z) setShown(z.scale);
      try {
        localStorage.setItem(ZOOM_KEY(kind), JSON.stringify(z));
      } catch {}
    },
    [kind],
  );
  // a pinch on the trackpad (Chrome / Edge send it as a ctrl + wheel, WebKit as gesture events) or ctrl + wheel
  // zooms the document, around the pointer, instead of the whole window
  const focus = useRef<{ x: number; y: number } | null>(null);
  useEffect(() => {
    let frame = 0;
    let factor = 1;
    const apply = () => {
      frame = 0;
      const { shown: cur, setZoom: set } = zoomRef.current;
      const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, cur * factor));
      factor = 1;
      if (Math.abs(next - cur) > 0.001) set({ scale: Math.round(next * 1000) / 1000 });
    };
    const queue = (f: number, x: number, y: number) => {
      factor *= f;
      focus.current = { x, y };
      if (!frame) frame = requestAnimationFrame(apply);
    };
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) {
        focus.current = null;
        return;
      }
      e.preventDefault();
      // a pinch gives small deltas, a mouse wheel notch ~100: both end up as a gentle factor
      queue(Math.exp(-Math.max(-25, Math.min(25, e.deltaY)) * 0.006), e.clientX, e.clientY);
    };
    let base = 1;
    let last = 1;
    const onGestureStart = (e: Event) => {
      e.preventDefault();
      base = zoomRef.current.shown;
      last = 1;
    };
    const onGestureChange = (e: Event) => {
      e.preventDefault();
      const g = e as Event & { scale: number; clientX: number; clientY: number };
      const target = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, base * g.scale));
      queue(target / (zoomRef.current.shown * factor) || 1, g.clientX, g.clientY);
      last = g.scale;
    };
    const onGestureEnd = (e: Event) => {
      e.preventDefault();
      void last;
    };
    window.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("gesturestart", onGestureStart, { passive: false } as AddEventListenerOptions);
    window.addEventListener("gesturechange", onGestureChange, { passive: false } as AddEventListenerOptions);
    window.addEventListener("gestureend", onGestureEnd, { passive: false } as AddEventListenerOptions);
    return () => {
      window.removeEventListener("wheel", onWheel);
      window.removeEventListener("gesturestart", onGestureStart);
      window.removeEventListener("gesturechange", onGestureChange);
      window.removeEventListener("gestureend", onGestureEnd);
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);
  // buttons and keys zoom around the reading line
  const zoomFrom = useCallback(
    (z: Zoom) => {
      focus.current = null;
      setZoom(z);
    },
    [setZoom],
  );
  // ⌘ / Ctrl with + − 0 zoom the document instead of the whole window
  const zoomRef = useRef({ shown, setZoom });
  zoomRef.current = { shown, setZoom };
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const { shown: cur, setZoom: set } = zoomRef.current;
      focus.current = null;
      const near = (dir: 1 | -1) => (dir > 0 ? ZOOM_STEPS.find((x) => x > cur + 0.001) ?? ZOOM_STEPS[ZOOM_STEPS.length - 1] : [...ZOOM_STEPS].reverse().find((x) => x < cur - 0.001) ?? ZOOM_STEPS[0]);
      if (e.key === "=" || e.key === "+") set({ scale: near(1) });
      else if (e.key === "-") set({ scale: near(-1) });
      else if (e.key === "0") set({ scale: 1 });
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // a book just imported may still have its text read in the background: ask again until it is
  useEffect(() => {
    if (material?.id !== id || material.status !== "extracting") return;
    const timer = setTimeout(() => {
      api
        .material(id)
        .then((m) => setMaterial((cur) => (cur?.id === m.id ? { ...cur, ...m } : cur)))
        .catch(() => {});
    }, EXTRACT_POLL_MS);
    return () => clearTimeout(timer);
  }, [id, material]);

  useEffect(() => {
    setMaterial(null);
    setError(null);
    setMenu(false);
    setAt(storedAt(id));
    api
      .material(id)
      .then((m) => {
        setMaterial(m);
        setTitle(m.title);
      })
      .catch((e: RequestError) => setError(e.body.detail));
  }, [id]);

  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => !menuBox.current?.contains(e.target as Node) && setMenu(false);
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setMenu(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  // a page asked for from the sidebar: brought to the top once the material is on screen (a material still loading
  // opens there anyway: App stored it as the reading position)
  useEffect(() => {
    if (!jump || jump.id !== id || material?.id !== id) return;
    const n = jump.n;
    const go = () => {
      if (gotoRef.current) return gotoRef.current(n);
      document.querySelector(`.prose-reader [data-at="${n}"]`)?.scrollIntoView({ block: "start" });
    };
    requestAnimationFrame(go);
  }, [jump, id, material?.id]);

  // the toolbar's height, for the folded sidebar floating over the page: the bar's colour runs on behind it
  const head = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const el = head.current;
    if (!el) return;
    const root = document.documentElement;
    const measure = () => root.style.setProperty("--reader-head-h", `${el.offsetHeight}px`);
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => {
      ro?.disconnect();
      root.style.removeProperty("--reader-head-h");
    };
  }, [material?.id]);

  const outline = useMemo(() => (info ? outlineRows(info.outline) : []), [info]);
  const [marking, setMarking] = useState(false);
  const marked = bookmarks.some((b) => b.unit_no === at);
  const toggleMark = async () => {
    if (!material || marking) return;
    setMarking(true);
    setError(null);
    try {
      const chapter = material.kind === "pdf" ? chapterAt(outline, at) : material.kind === "md" ? headingAt(material.units, at) : null;
      onBookmarks?.(material.id, marked ? await api.removeBookmark(material.id, at) : await api.addBookmark(material.id, at, chapter));
    } catch (e) {
      setError((e as RequestError).body?.detail ?? String(e));
    } finally {
      setMarking(false);
    }
  };

  const position = useRef(onPosition);
  position.current = onPosition;
  const onAt = useCallback(
    (n: number) => {
      setAt(n);
      position.current?.(id, n);
      try {
        localStorage.setItem(POSITION_KEY(id), String(n));
      } catch {}
    },
    [id],
  );

  if (!material) return <div className="doc">{error ? <div className="error">{error}</div> : <p className="muted">{t("material.loading")}</p>}</div>;

  const category = library.categories.find((c) => c.id === material.category_id);
  const unitWord = t(material.unit === "page" ? "material.unit.page" : "material.unit.line");

  const patch = async (p: { title?: string; category_id?: string | null }) => {
    setError(null);
    try {
      const m = await api.updateMaterial(material.id, p);
      setMaterial({ ...material, ...m });
      await onLibraryChange();
    } catch (e) {
      setError((e as RequestError).body.detail);
    }
  };

  const remove = async () => {
    await api.deleteMaterial(material.id);
    await onLibraryChange();
    onNavigate({ page: "home" });
  };

  return (
    <div className="reader-page">
      <header className="reader-head" ref={head}>
        <div className="reader-title">
          <span className="reader-crumb">{category?.name ?? t("common.uncategorized")}</span>
          <input
            aria-label={t("material.name")}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={() => (title.trim() && title.trim() !== material.title ? patch({ title: title.trim() }) : setTitle(material.title))}
            onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
          />
        </div>
        <StudyBadge study={study} onAction={studyAction} />
        <button
          type="button"
          className={`icon-btn reader-mark ${marked ? "on" : ""}`}
          aria-pressed={marked}
          aria-label={t(marked ? "reader.bookmark.remove" : "reader.bookmark.add")}
          title={t(marked ? "reader.bookmark.remove" : "reader.bookmark.add")}
          onClick={toggleMark}
          disabled={marking}
        >
          <Icon.bookmark filled={marked} />
        </button>
        {info && info.outline.length > 0 && <OutlineMenu info={info} at={at} onGoto={(p, y) => gotoRef.current?.(p, y)} />}
        <div className="reader-more" ref={menuBox}>
          <button type="button" className="icon-btn" aria-haspopup="dialog" aria-expanded={menu} aria-label={t("reader.more")} onClick={() => setMenu((v) => !v)}>
            <Icon.more />
          </button>
          {menu && (
            <MaterialCard
              material={material}
              info={info}
              library={library}
              zoom={<ZoomBar zoom={zoom} shown={shown} pdf={material.kind === "pdf"} onZoom={zoomFrom} />}
              onCategory={(id) => patch({ category_id: id })}
              onDelete={remove}
            />
          )}
        </div>
      </header>
      {error && <div className="error reader-page-error">{error}</div>}
      <div className="reader-body" aria-label={t("material.content")}>
        {material.kind === "pdf" ? (
          <PdfReader material={material} at={at} zoom={zoom} focus={focus} gotoRef={gotoRef} onAt={onAt} onScale={setShown} />
        ) : (
          <TextReader material={material} at={at} scale={"scale" in zoom ? zoom.scale : 1} onAt={onAt} />
        )}
      </div>
      <Composer material={material} at={at} unitWord={unitWord} onOpenSession={onOpenSession} />
    </div>
  );
}
