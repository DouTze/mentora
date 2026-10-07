// The start page does two things only: find something new to read, or pick up a reading left unfinished.
//
// Finding happens on the bookshelf: the backend's scout guesses what this person wants to read from what they
// imported, read and typed, and finds those books on their disk. The box above it takes a wish ("強化學習"):
// typing filters the shelf at once with a plain keyword search, Enter saves the wish and lets the scout choose
// again in the background. Nothing waits on the model: the shelf always shows something, and while the scout
// works the companion only murmurs that it is getting ready. Files can still be dropped or picked by hand.
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, RequestError } from "../api";
import type { Library, SessionListItem, ShelfBook, ShelfView } from "../types";
import type { Route } from "../route";
import { Icon } from "../components/Icons";
import { Bookshelf } from "../components/Bookshelf";
import { useLocale, useT, type Key } from "../i18n";

interface Props {
  library: Library;
  onLibraryChange: () => Promise<unknown>;
  onNavigate: (r: Route) => void;
  onOpenSession: (id: string) => void;
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [["day", 86400], ["hour", 3600], ["minute", 60]];

/** "3 hours ago" / "3 小時前"; under a minute reads as "now" */
function ago(iso: string, locale: string): string {
  const secs = (Date.parse(iso) - Date.now()) / 1000;
  const fmt = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [unit, size] of UNITS) if (Math.abs(secs) >= size) return fmt.format(Math.round(secs / size), unit);
  return fmt.format(0, "second");
}

const MURMURS: Key[] = ["home.murmur.1", "home.murmur.2", "home.murmur.3", "home.murmur.4", "home.murmur.5"];
const POLL_MS = 1500;
const TYPE_DEBOUNCE_MS = 260;
const bookKey = (s: SessionListItem) => s.source?.material_id ?? s.content_title;
let kicked = false; // the first empty shelf of an app run asks the scout once, not on every visit

/** One line that changes every few seconds, so preparing feels like someone at work rather than a spinner. */
function Murmur() {
  const t = useT();
  const [i, setI] = useState(() => Math.floor(Math.random() * MURMURS.length));
  useEffect(() => {
    const id = setInterval(() => setI((n) => (n + 1) % MURMURS.length), 2600);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="murmur" aria-live="polite">
      <span className="murmur-dot" aria-hidden="true" />
      <span key={i} className="murmur-text">
        {t(MURMURS[i])}
      </span>
    </span>
  );
}

export function HomePage({ library, onLibraryChange, onNavigate, onOpenSession }: Props) {
  const t = useT();
  const locale = useLocale();
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [view, setView] = useState<ShelfView | null>(null);
  const [text, setText] = useState("");
  const [typed, setTyped] = useState<ShelfBook[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [areaName, setAreaName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  // the page scrolls the window, or .shell under the desktop title bar; either one lifts the veil
  useEffect(() => {
    const onScroll = (e: Event) => {
      const top = e.target instanceof Element ? e.target.scrollTop : window.scrollY;
      setScrolled(top > 24);
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => document.removeEventListener("scroll", onScroll, { capture: true });
  }, []);

  useEffect(() => {
    api.sessions().then(setSessions).catch(() => {});
    api.shelf()
      .then(async (v) => {
        if (v.status === "ready" && v.books.length === 0 && !kicked) {
          kicked = true;
          v = await api.shelfQuery("");
        }
        setView(v);
      })
      .catch(() => {});
  }, []);

  // while the scout works, look again now and then; its result replaces the shelf when it is ready
  useEffect(() => {
    if (view?.status !== "preparing") return;
    const id = setTimeout(() => api.shelf().then(setView).catch(() => {}), POLL_MS);
    return () => clearTimeout(id);
  }, [view]);

  // typing filters the shelf right away (keyword search, no model)
  useEffect(() => {
    const q = text.trim();
    if (!q) {
      setTyped(null);
      return;
    }
    const id = setTimeout(() => api.shelfSearch(q).then((r) => setTyped(r.books)).catch(() => {}), TYPE_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [text]);

  // once the scout has answered the wish that was sent, its choice is what the shelf shows
  useEffect(() => {
    if (view?.status === "ready" && view.query && view.query === text.trim()) setTyped(null);
  }, [view]);

  const preparing = view?.status === "preparing";
  const books = typed && typed.length > 0 ? typed : view?.books ?? [];
  const chosen = books.filter((b) => selected.has(b.id));

  const toggle = (id: string) => {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  // a reading area is named after what the chosen books share: their tendency, else the wish
  const suggestedName = useMemo(() => {
    const counts = new Map<string, number>();
    for (const b of chosen) if (b.tendency) counts.set(b.tendency, (counts.get(b.tendency) ?? 0) + 1);
    const top = [...counts].sort((a, b) => b[1] - a[1])[0]?.[0];
    return top ?? view?.query ?? view?.tendencies[0] ?? t("shelf.area.default");
  }, [chosen.map((b) => b.id).join(), view]);
  useEffect(() => setAreaName(suggestedName), [suggestedName]);

  const ask = async (wish: string) => {
    const q = wish.trim();
    if (!q) return;
    try {
      setView(await api.shelfQuery(q));
    } catch (e) {
      setError((e as RequestError).body.detail);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    ask(text);
  };

  const importFiles = async (files: File[]) => {
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      let lastId = "";
      for (const f of files) lastId = (await api.importMaterial(f, null)).id;
      await onLibraryChange();
      onNavigate({ page: "material", id: lastId });
    } catch (e) {
      setError((e as RequestError).body.detail);
      await onLibraryChange();
    } finally {
      setBusy(false);
    }
  };

  const importChosen = async () => {
    if (chosen.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.shelfImport(chosen.map((b) => b.id), chosen.length > 1 ? areaName.trim() || suggestedName : null);
      await onLibraryChange();
      onNavigate({ page: "material", id: r.materials[0].id });
    } catch (e) {
      setError((e as RequestError).body.detail);
      setBusy(false);
    }
  };

  // one row per book: the most recently touched unfinished reading of each material
  const active = sessions
    .filter((s) => s.status === "active")
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .filter((s, i, all) => all.findIndex((o) => bookKey(o) === bookKey(s)) === i)
    .slice(0, 3);
  const empty = library.materials.length === 0;
  const shelfLabel = typed && typed.length > 0 ? t("shelf.label.matching", { q: text.trim() }) : view?.query ? t("shelf.label.wish", { q: view.query }) : t("shelf.label.forYou");

  return (
    <div
      className="home"
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        importFiles([...e.dataTransfer.files]);
      }}
    >
      <h1 className="home-title">{t(empty ? "home.title.empty" : "home.title")}</h1>

      <form className={`home-ask ${over ? "over" : ""}`} onSubmit={submit}>
        <button type="button" className="home-ask-btn" title={t("home.attach")} aria-label={t("home.attach")} disabled={busy} onClick={() => fileInput.current?.click()}>
          <Icon.attach />
        </button>
        <input
          className="home-ask-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={over ? t("home.drop.release") : t("home.ask.placeholder")}
          aria-label={t("home.ask.label")}
          maxLength={200}
        />
        <button type="submit" className="home-ask-send" aria-label={t("home.ask.send")} disabled={!text.trim()}>
          <Icon.arrowUp />
        </button>
        <input ref={fileInput} type="file" accept=".pdf,.md,.markdown,.txt" multiple hidden onChange={(e) => { importFiles([...(e.target.files ?? [])]); e.target.value = ""; }} />
      </form>

      <div className="home-status">
        {busy ? (
          <span className="murmur">
            <span className="murmur-dot" aria-hidden="true" />
            <span className="murmur-text">{t("home.importing")}</span>
          </span>
        ) : preparing || !view ? (
          <Murmur />
        ) : view.tendencies.length > 0 ? (
          <span className="tendencies">
            {view.tendencies.map((x) => (
              <button
                key={x}
                type="button"
                className="chip"
                onClick={() => {
                  setText(x);
                  ask(x);
                }}
              >
                {x}
              </button>
            ))}
          </span>
        ) : null}
      </div>

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}

      {(books.length > 0 || preparing || !view) && (
        <section className="home-shelf">
          <h2 className="home-label">{shelfLabel}</h2>
          <Bookshelf books={books} loading={preparing || !view} selected={selected} onToggle={toggle} />
        </section>
      )}

      {chosen.length > 0 && (
        <div className="shelf-bar" role="region" aria-label={t("shelf.bar.label")}>
          {chosen.length === 1 ? (
            <span className="shelf-bar-text grow">{chosen[0].title}</span>
          ) : (
            <label className="shelf-bar-area grow">
              <span>{t("shelf.area.prefix", { count: chosen.length })}</span>
              <input value={areaName} onChange={(e) => setAreaName(e.target.value)} maxLength={40} aria-label={t("shelf.area.name")} />
            </label>
          )}
          <button type="button" className="ghost" onClick={() => setSelected(new Set())} disabled={busy}>
            {t("common.cancel")}
          </button>
          <button type="button" className="primary" onClick={importChosen} disabled={busy}>
            {t(chosen.length === 1 ? "shelf.bar.read" : "shelf.bar.area")}
          </button>
        </div>
      )}

      {empty && !preparing && view && books.length === 0 && (
        <p className="home-hint">
          {t("home.empty.before")}
          <button className="link" onClick={() => onNavigate({ page: "learn" })}>
            {t("home.empty.link")}
          </button>
          {t("home.empty.after")}
        </p>
      )}

      {active.length > 0 && (
        <section className="home-resume">
          <h2 className="home-label">{t("home.active")}</h2>
          <ul>
            {active.map((s) => (
              <li key={s.id}>
                <button onClick={() => onOpenSession(s.id)}>
                  <span className="grow">
                    <span className="home-resume-title">{s.source ? s.source.material_title : s.content_title}</span>
                    <span className="home-resume-meta">
                      {s.source ? `${s.source.label} · ` : ""}
                      {ago(s.updated_at, locale)}
                    </span>
                  </span>
                  <Icon.chevron />
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* on arrival only the box stands out; what lies below fades into the page until the reader scrolls or picks */}
      <div className={`home-veil ${scrolled || selected.size > 0 ? "lifted" : ""}`} aria-hidden="true" />
    </div>
  );
}
