// 閱讀器: an imported material opened like a book in the app — PDF pages laid out as paper, Markdown / text set as an
// article — with one box at the foot to ask about the page being read, or to go into 導讀 / 出題 from there.
//
// 導讀 prepares only the pages around the one being read (設定 › 導讀 sets how many), when it is pressed: the pages
// give way to the 導讀 mode (components/GuideTour), which scrolls on into the pages before and after. A question is
// answered from the 主題 and guides of its page once the 導讀 mode has prepared them, else from the page itself.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, RequestError } from "../api";
import type { Bookmark, Guide, Library, MaterialDetail, PdfInfo, PdfOutlineItem, PdfPageLayer, Quote, ReaderQuestion, SessionView, TourView } from "../types";
import type { ReaderView, Route } from "../route";
import { Icon } from "../components/Icons";
import { PdfLayer } from "../components/PdfLayer";
import { RichText } from "../components/RichText";
import { Select } from "../components/Select";
import { Working } from "../components/Working";
import { quoteAnchor } from "../components/ReadingPanel";
import { GuideTour, topicAnchor } from "../components/GuideTour";
import { LearnPage } from "./Learn";
import { FINDING, MIN_SWITCH_MS, prepared, progressOf, type ModeKind } from "../readerMode";
import { useT } from "../i18n";
import { composing } from "../ime";
import { usePrefs } from "../prefs";

interface Props {
  id: string;
  /** 閱讀模式: the book switched to the 導讀 or 出題 of one reading. The bar and the question box stay; only the
   *  pages give way (kept as they were underneath, so switching back finds the reader where it was) */
  view?: ReaderView | null;
  library: Library;
  onLibraryChange: () => Promise<unknown>;
  onNavigate: (r: Route) => void;
  onOpenSession: (id: string, opts?: { quiz?: boolean; material?: string }) => void;
  /** this material's bookmarks (App keeps every book's, the sidebar lists them) */
  bookmarks?: Bookmark[];
  onBookmarks?: (id: string, marks: Bookmark[]) => void;
  /** a page to bring into view, asked for from outside (a bookmark in the sidebar); `k` makes each ask new */
  jump?: { id: string; n: number; k: number } | null;
  /** the page (line) being read, as it changes */
  onPosition?: (id: string, n: number) => void;
}

const LEAVE_MS = 320;              // the 導讀 mode fading out over the pages (styles.css: tour-out)
const ENTER_MS = 460;              // and fading in: the pages stay drawn underneath until it has
const FILL_STEP_MS = 300;        // the fill's steps as the mode is prepared
const FILL_SETTLE_MS = 250;      // its last step at least: the mode takes the screen when the fill is full
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

// -- the question box ---------------------------------------------------------------------------
/** 導讀 and 出題 are switches: on, the reader shows that mode; pressed again, the pages come back. 導讀 opens the 導讀
 * mode at the page being read; 出題 the questions of the part being read (a reading started or taken up again). A
 * switch into a mode fills its button from the left, like a charging battery, as far as the mode is prepared; 導讀
 * pressed while it fills stops preparing it. */
function Composer({ material, at, unitWord, mode, fill, onPress, onGuide, onCancelGuide, onOpenSession, onMode }: {
  material: MaterialDetail;
  at: number;
  unitWord: string;
  /** the mode on screen */
  mode: ReaderView | null;
  /** a switch under way: its button fills from the left as the mode is prepared (0–1) */
  fill: { kind: ModeKind; p: number; ms: number } | null;
  /** a press that has to wait for the reading (found or started first); null when that failed */
  onPress: (kind: ModeKind | null) => void;
  /** 導讀: the 導讀 mode opened at this page; resolves to why it could not be, or null */
  onGuide: () => Promise<string | null>;
  onCancelGuide: () => void;
  onOpenSession: Props["onOpenSession"];
  onMode: (v: ReaderView | null) => void;
}) {
  const t = useT();
  const [text, setText] = useState("");
  const [qa, setQa] = useState<ReaderQuestion[]>([]);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
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

  const learn = async (kind: ModeKind) => {
    setError(null);
    if (kind === "guide") {
      if (fill?.kind === "guide") return onCancelGuide();
      if (mode?.kind === "tour" || mode?.kind === "guide") return onMode(null);
      const why = await onGuide();
      if (why) setError(why);
      return;
    }
    if (mode?.kind === "quiz") return onMode(null);
    if (mode?.kind === "guide") return onMode({ kind, session: mode.session });
    onPress(kind);
    try {
      const s = await api.readerLearn(material.id, at);
      onOpenSession(s.id, { quiz: true, material: material.id });
    } catch (e) {
      setError((e as RequestError).body?.detail ?? String(e));
      onPress(null);
    }
  };
  const guideOn = mode?.kind === "guide" || mode?.kind === "tour";

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !composing(e)) {
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
          <button type="button" className={`reader-action ${guideOn && !fill ? "on" : ""} ${fill?.kind === "guide" ? "filling" : ""}`} style={fill?.kind === "guide" ? ({ "--fill": fill.p, "--fill-ms": `${fill.ms}ms` } as React.CSSProperties) : undefined} aria-pressed={guideOn} aria-busy={fill?.kind === "guide" || undefined} onClick={() => learn("guide")} disabled={(!!fill && fill.kind !== "guide") || blocked} title={fill?.kind === "guide" ? t("tour.cancel") : guideOn ? t("reader.mode.off") : t("reader.guide.hint", { unit: unitWord })}>
            <Icon.book />
            {t("reader.guide")}
          </button>
          <button type="button" className={`reader-action ${mode?.kind === "quiz" && !fill ? "on" : ""} ${fill?.kind === "quiz" ? "filling" : ""}`} style={fill?.kind === "quiz" ? ({ "--fill": fill.p, "--fill-ms": `${fill.ms}ms` } as React.CSSProperties) : undefined} aria-pressed={mode?.kind === "quiz"} aria-busy={fill?.kind === "quiz" || undefined} onClick={() => learn("quiz")} disabled={!!fill || blocked} title={mode?.kind === "quiz" ? t("reader.mode.off") : t("reader.quiz.hint", { unit: unitWord })}>
            <Icon.quiz />
            {t("reader.quiz")}
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

/** What a part of a 導讀 is called in the table of contents unless renamed: the start of its 「這一段在講」, else
 * its heading. */
function partDefault(q: Quote, g: Guide | undefined): string | null {
  const s = (g?.topic_summary ?? "").replace(/\s+/g, " ").trim();
  if (s) {
    // the subject, without the summary's 「本主題探討…」 lead-in
    const first = s
      .split(/(?<=[。！？；.!?;])/)[0]
      .replace(/[。．.；;]$/, "")
      .replace(/^(本(主題|段落?|節|章|部分)|這一?段)(主要)?(在)?(探討|說明|介紹|講述|描述|討論|談|講)(了|的是)?[，,：:]?\s*/, "");
    return first.length > 28 ? `${first.slice(0, 28)}…` : first;
  }
  return q.heading;
}

/** 閱讀模式's table of contents: the reading's 段落 (quotes), each by a short description the reader can edit
 * (by default the start of what the guide says the part is about). The ones already guided are on the page above
 * the current one, so picking one scrolls to it; the ones still ahead are listed, not yet open. */
function GuideOutline({ material, session }: { material: string; session: SessionView | null }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [names, setNames] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    api
      .labels("part")
      .then(setNames)
      .catch(() => {});
    list.current?.querySelector(".current")?.scrollIntoView({ block: "center" });
    const onDown = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && !(e.target instanceof HTMLInputElement) && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const rd = session?.reading ?? null;
  const reached = rd ? (rd.done ? rd.total - 1 : rd.index) : -1;
  const rename = (ref: string, label: string | null) => {
    setEditing(null);
    if (label === null) return;
    api
      .setLabel("part", ref, label)
      .then(setNames)
      .catch(() => {});
  };
  return (
    <div className="reader-more" ref={box}>
      <button type="button" className="icon-btn" aria-haspopup="menu" aria-expanded={open} aria-label={t("reader.guide.toc")} title={t("reader.guide.toc")} onClick={() => setOpen((v) => !v)} disabled={!rd}>
        <Icon.toc />
      </button>
      {open && rd && (
        <div className="menu reader-menu reader-toc guide-toc" role="menu" ref={list}>
          {rd.quotes.map((q, i) => {
            const ref = `${material}:${q.unit_id ?? q.id}`;
            const fallback = partDefault(q, rd.guides[String(i)]) ?? t("reader.guide.part", { n: i + 1 });
            const name = names[ref] ?? fallback;
            const current = !rd.done && i === rd.index;
            if (editing === ref) {
              return (
                <PartNameInput key={q.id} num={i + 1} initial={names[ref] ?? fallback} placeholder={fallback} label={t("reader.guide.rename")} onDone={(v) => rename(ref, v === fallback ? "" : v)} />
              );
            }
            return (
              <div key={q.id} className={`guide-toc-row ${current ? "current" : ""}`}>
                <button
                  type="button"
                  role="menuitem"
                  className={current ? "current" : undefined}
                  disabled={i > reached}
                  title={i > reached ? t("reader.guide.ahead") : undefined}
                  onClick={() => {
                    setOpen(false);
                    document.getElementById(quoteAnchor(i))?.scrollIntoView({ block: "start" });
                  }}
                >
                  <span className="toc-num">{i + 1}</span>
                  <span className="toc-title">{name}</span>
                </button>
                <button type="button" className="guide-toc-edit" aria-label={t("reader.guide.rename")} title={t("reader.guide.rename")} onClick={() => setEditing(ref)}>
                  <Icon.pencil />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function PartNameInput({ num, initial, placeholder, label, onDone }: { num: number; initial: string; placeholder: string; label: string; onDone: (v: string | null) => void }) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);
  const finish = (v: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(v === null ? null : v.trim());
  };
  return (
    <div className="guide-toc-row naming">
      <span className="toc-num">{num}</span>
      <input
        autoFocus
        aria-label={label}
        placeholder={placeholder}
        value={value}
        maxLength={80}
        onChange={(e) => setValue(e.target.value)}
        onFocus={(e) => e.target.select()}
        onBlur={() => finish(value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !composing(e)) finish(value);
          if (e.key === "Escape") finish(null);
        }}
      />
    </div>
  );
}

/** The 導讀 mode's table of contents: every 主題 of the run on screen, named as in GuideOutline. Picking one brings it
 * into view (one not written yet too: the work turns towards it). */
function TourOutline({ material, view }: { material: string; view: TourView | null }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [names, setNames] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    api
      .labels("part")
      .then(setNames)
      .catch(() => {});
    list.current?.querySelector(".current")?.scrollIntoView({ block: "center" });
    const onDown = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && !(e.target instanceof HTMLInputElement) && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const rows = (view?.windows ?? []).flatMap((w) => w.topics.map((tp) => ({ sid: w.session_id, tp })));
  const rename = (ref: string, label: string | null) => {
    setEditing(null);
    if (label === null) return;
    api
      .setLabel("part", ref, label)
      .then(setNames)
      .catch(() => {});
  };
  return (
    <div className="reader-more" ref={box}>
      <button type="button" className="icon-btn" aria-haspopup="menu" aria-expanded={open} aria-label={t("reader.guide.toc")} title={t("reader.guide.toc")} onClick={() => setOpen((v) => !v)} disabled={!rows.length}>
        <Icon.toc />
      </button>
      {open && (
        <div className="menu reader-menu reader-toc guide-toc" role="menu" ref={list}>
          {rows.map(({ sid, tp }, i) => {
            const ref = `${material}:${tp.unit_id ?? `${sid}:${tp.index}`}`;
            const fallback = partDefault({ heading: tp.heading } as Quote, tp.guide ?? undefined) ?? t("reader.guide.part", { n: i + 1 });
            const current = view?.focus?.session_id === sid && view.focus.index === tp.index;
            if (editing === ref) {
              return <PartNameInput key={ref} num={i + 1} initial={names[ref] ?? fallback} placeholder={fallback} label={t("reader.guide.rename")} onDone={(v) => rename(ref, v === fallback ? "" : v)} />;
            }
            return (
              <div key={ref} className={`guide-toc-row ${current ? "current" : ""} ${tp.guide ? "" : "pending"}`}>
                <button
                  type="button"
                  role="menuitem"
                  className={current ? "current" : undefined}
                  title={tp.guide ? undefined : t(tp.state === "working" ? "tour.working" : tp.state === "failed" ? "tour.failed" : "tour.queued")}
                  onClick={() => {
                    setOpen(false);
                    document.getElementById(topicAnchor(sid, tp.index))?.scrollIntoView({ block: "start", behavior: "smooth" });
                  }}
                >
                  <span className="toc-num">{i + 1}</span>
                  <span className="toc-title">{names[ref] ?? fallback}</span>
                </button>
                <button type="button" className="guide-toc-edit" aria-label={t("reader.guide.rename")} title={t("reader.guide.rename")} onClick={() => setEditing(ref)}>
                  <Icon.pencil />
                </button>
              </div>
            );
          })}
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
export function MaterialPage({ id, view = null, library, onLibraryChange, onNavigate, onOpenSession, bookmarks = NO_MARKS, onBookmarks, jump = null, onPosition }: Props) {
  const t = useT();
  const [material, setMaterial] = useState<MaterialDetail | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);
  const [at, setAt] = useState(() => storedAt(id));
  const menuBox = useRef<HTMLDivElement>(null);
  const gotoRef = useRef<Goto | null>(null);
  // 閱讀模式. `view` (the route) is the mode asked for; `onScreen` the one on screen. A mode asked for is laid out out
  // of sight while its reading is found, loaded and prepared (its button filling as it goes, for at least
  // MIN_SWITCH_MS), and takes the screen once it has something to show; the pages or the mode before stay until then.
  // Back to the pages is at once.
  const [onScreen, setOnScreen] = useState<ReaderView | null>(null);
  const [switching, setSwitching] = useState<{ kind: ModeKind; since: number } | null>(null);
  const [sessions, setSessions] = useState<Record<string, SessionView>>({});
  const [failed, setFailed] = useState<string | null>(null);
  // the 導讀 mode is one layer whichever window it shows: a window picked while it is on screen scrolls there
  const keyOf = (v: ReaderView | null) => (v ? (v.kind === "tour" ? "tour" : `${v.kind}:${v.session}`) : "");
  const buttonOf = (v: ReaderView): ModeKind => (v.kind === "quiz" ? "quiz" : "guide");
  const viewKey = keyOf(view);
  const shownKey = keyOf(onScreen);
  const asked = view && viewKey !== shownKey ? view : null;
  // 導讀 mode: the page it was opened at (its 主題 is shown first), how far that 主題 is, the run on screen (for the
  // table of contents), the page of the 主題 on screen (for the question box), and the layer fading out after it
  const [tourPrep, setTourPrep] = useState({ p: FINDING, ready: false });
  const [tourView, setTourView] = useState<TourView | null>(null);
  const [tourAt, setTourAt] = useState<number | null>(null);
  const [leaving, setLeaving] = useState<ReaderView | null>(null);
  const openSeq = useRef(0);
  useEffect(() => {
    if (!view) {
      if (onScreen?.kind === "tour") {
        setLeaving(onScreen);   // fades out over the pages; its work stops now
        api.tourPause(id).catch(() => {});
      }
      setOnScreen(null);
      setSwitching(null);
      return;
    }
    if (view.kind === "tour") setLeaving(null);
    if (viewKey !== shownKey) {
      if (view.kind === "tour" && !switching) setTourPrep({ p: FINDING, ready: false });
      setSwitching((w) => (w?.kind === buttonOf(view) ? w : { kind: buttonOf(view), since: Date.now() }));
    } else if (onScreen && (view.session !== onScreen.session || view.at !== onScreen.at)) setOnScreen(view);   // another window of the 導讀 mode
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey, view?.session, view?.at]);
  useEffect(() => {
    if (!leaving) return;
    const timer = setTimeout(() => setLeaving(null), LEAVE_MS);
    return () => clearTimeout(timer);
  }, [leaving]);
  // the pages stay drawn under the 導讀 mode while it fades in, then are put away; they come back at once
  const [pagesOff, setPagesOff] = useState(false);
  useEffect(() => {
    if (!onScreen) return setPagesOff(false);
    if (onScreen.kind !== "tour") return setPagesOff(true);
    const timer = setTimeout(() => setPagesOff(true), ENTER_MS);
    return () => clearTimeout(timer);
  }, [onScreen?.kind]);
  const askedSession = asked && asked.kind !== "tour" ? (sessions[viewKey] ?? null) : null;
  const ready = !!asked && (failed === viewKey || (asked.kind === "tour" ? tourPrep.ready : !!askedSession && prepared(asked.kind, askedSession)));
  // the last stretch of the fill runs out the switch's minimum time, then the mode takes the screen
  const finishMs = useMemo(
    () => (ready && switching ? Math.max(MIN_SWITCH_MS - (Date.now() - switching.since), FILL_SETTLE_MS) : 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ready, viewKey],
  );
  const fill = switching
    ? { kind: switching.kind, p: ready ? 1 : !asked ? FINDING : asked.kind === "tour" ? tourPrep.p : progressOf(asked.kind, askedSession), ms: ready ? finishMs : FILL_STEP_MS }
    : null;
  useEffect(() => {
    if (!ready || !asked || !switching) return;
    const t = setTimeout(() => {
      setOnScreen(asked);
      setSwitching(null);
    }, finishMs);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, viewKey]);
  // zooming is for the pages: not while they are switched away
  const viewing = useRef(false);
  viewing.current = !!onScreen;
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
      if (!e.ctrlKey || viewing.current) {
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
      if (viewing.current) return;
      e.preventDefault();
      base = zoomRef.current.shown;
      last = 1;
    };
    const onGestureChange = (e: Event) => {
      if (viewing.current) return;
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
      if (!(e.metaKey || e.ctrlKey) || e.altKey || viewing.current) return;
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

  // 設定 › 導讀 decides the language for every book: the open book takes it, so a reading started here (or by the
  // companion) is written in it; readings already started keep theirs
  const { guideLang, guideSpan } = usePrefs();
  const translateZh = guideLang === "zh";
  useEffect(() => {
    if (material?.id !== id || material.translate_zh === translateZh) return;
    api
      .updateMaterial(id, { translate_zh: translateZh })
      .then((m) => setMaterial((cur) => (cur?.id === m.id ? { ...cur, ...m } : cur)))
      .catch(() => {});
  }, [id, material?.id, material?.translate_zh, translateZh]);

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

  // 導讀 pressed: the window around this page is found or made, then the mode is laid out out of sight until the
  // 主題 of this page is written (the button filling meanwhile); pressed again while it fills, nothing is prepared
  const openTour = async (): Promise<string | null> => {
    const seq = ++openSeq.current;
    setTourPrep({ p: FINDING, ready: false });
    setSwitching({ kind: "guide", since: Date.now() });
    try {
      const v = await api.tourOpen(material.id, at, guideSpan);
      if (seq !== openSeq.current) return null;
      const sid = v.focus?.session_id;
      if (!sid) throw new Error("no window");
      onNavigate({ page: "material", id: material.id, view: { kind: "tour", session: sid, at } });
      return null;
    } catch (e) {
      if (seq !== openSeq.current) return null;
      setSwitching(null);
      return (e as RequestError).body?.detail ?? String(e);
    }
  };
  const cancelTour = () => {
    openSeq.current++;
    setSwitching(null);
    api.tourPause(material.id).catch(() => {});
    if (view?.kind === "tour" && onScreen?.kind !== "tour") onNavigate({ page: "material", id: material.id });
  };
  // the 導讀 layer: on screen, being prepared, or fading out — one layer throughout, so it keeps its place
  const tour = onScreen?.kind === "tour" ? onScreen : asked?.kind === "tour" ? asked : leaving;

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
            onKeyDown={(e) => e.key === "Enter" && !composing(e) && e.currentTarget.blur()}
          />
        </div>
        {!onScreen && (
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
        )}
        {onScreen?.kind === "tour" ? (
          <TourOutline material={material.id} view={tourView} />
        ) : onScreen ? (
          <GuideOutline material={material.id} session={sessions[shownKey] ?? null} />
        ) : (
          info && info.outline.length > 0 && <OutlineMenu info={info} at={at} onGoto={(p, y) => gotoRef.current?.(p, y)} />
        )}
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
      {/* the pages stay laid out (and scrolled) under the mode, so switching back is instant and in place */}
      <div className={`reader-body ${pagesOff ? "off" : ""}`} aria-label={t("material.content")} aria-hidden={onScreen ? true : undefined}>
        {material.kind === "pdf" ? (
          <PdfReader material={material} at={at} zoom={zoom} focus={focus} gotoRef={gotoRef} onAt={onAt} onScale={setShown} />
        ) : (
          <TextReader material={material} at={at} scale={"scale" in zoom ? zoom.scale : 1} onAt={onAt} />
        )}
      </div>
      {/* the mode on screen and the one being prepared, one list by key: the prepared one keeps its page when shown */}
      {tour && (
        <GuideTour
          key="tour"
          material={material}
          session={tour.session}
          at={tour.at ?? null}
          className={tour === leaving ? "leaving" : tour === onScreen ? "" : "loading"}
          onProgress={(p, ready) => setTourPrep((x) => (x.p === p && x.ready === ready ? x : { p, ready }))}
          onAt={setTourAt}
          onView={setTourView}
        />
      )}
      {[onScreen, asked].filter((v): v is ReaderView => !!v && v.kind !== "tour").map((v) => {
        const key = keyOf(v);
        return (
          <div className={`reader-body reader-mode ${key === shownKey ? "" : "loading"}`} key={key} aria-hidden={key === shownKey ? undefined : true}>
            <div className="reader-mode-col">
              <LearnPage
                embedded
                view={v.kind === "quiz" ? "quiz" : "guide"}
                sessionId={v.session}
                autoQuiz={v.kind === "quiz"}
                onSession={(x) => {
                  // a window of the 導讀 mode opened as a reading (from 學習紀錄): the 導讀 mode at it instead
                  if (x.workflow?.waiting === "tour") return onNavigate({ page: "material", id: material.id, view: { kind: "tour", session: x.id } });
                  // a reading from before the 導讀 mode, still being read: the 導讀 mode at its first page
                  if (v.kind === "guide" && x.state.phase === "reading" && !x.reading?.done && x.source?.start) {
                    const start = x.source.start;
                    api.tourOpen(material.id, start, guideSpan).then((t) => t.focus && onNavigate({ page: "material", id: material.id, view: { kind: "tour", session: t.focus.session_id, at: start } })).catch(() => {});
                    return;
                  }
                  setSessions((all) => (all[key] === x ? all : { ...all, [key]: x }));
                }}
                onError={() => setFailed(key)}
                onSessionChange={(sid) => onNavigate({ page: "material", id: material.id, view: { kind: v.kind, session: sid } })}
              />
            </div>
          </div>
        );
      })}
      <Composer
        material={material}
        at={onScreen?.kind === "tour" ? (tourAt ?? at) : at}
        unitWord={unitWord}
        mode={onScreen}
        fill={fill}
        onPress={(kind) => setSwitching(kind ? { kind, since: Date.now() } : null)}
        onGuide={openTour}
        onCancelGuide={cancelTour}
        onOpenSession={onOpenSession}
        onMode={(v) => onNavigate(v ? { page: "material", id: material.id, view: v } : { page: "material", id: material.id })}
      />
    </div>
  );
}
