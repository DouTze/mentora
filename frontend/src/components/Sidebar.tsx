import { useEffect, useRef, useState, type DragEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useT, type Key } from "../i18n";
import { api, RequestError } from "../api";
import { importFiles } from "../library";
import type { Bookmark, Bookmarks, Category, Library, Material, SessionListItem } from "../types";
import type { CompanionId } from "../companions";
import { useStored, type Route } from "../route";
import { CompanionDock, type Connection } from "./CompanionDock";
import { Icon, KindTag } from "./Icons";

const ACCEPT = ".pdf,.md,.markdown,.txt";
const UNSORTED = "__unsorted__";

/** What is being dragged inside the sidebar: a material (to another category) or a category (to another place). */
type Dragged = { kind: "material" | "category"; id: string; label: string; tag?: string };
type DropSpot = { key: string; categoryId: string | null };

/**
 * Dragging inside the sidebar, done with pointer events instead of HTML5 drag and drop: the cursor turns into a
 * grabbing hand the moment the button is pressed and stays so for the whole drag (a native drag shows the system's
 * own cursor), a small label follows the pointer, and nothing depends on the web view's file-drop handling.
 * A press that does not move more than a few pixels stays a click.
 */
function useSidebarDrag(onDrop: (d: Dragged, spot: DropSpot) => void) {
  const [ghost, setGhost] = useState<{ d: Dragged; x: number; y: number } | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const dragged = useRef(false);
  const drop = useRef(onDrop);
  drop.current = onDrop;

  const start = (d: Dragged) => (e: ReactPointerEvent) => {
    if (e.button !== 0 || e.pointerType === "touch") return;
    const x0 = e.clientX, y0 = e.clientY;
    let moving = false;
    let spot: DropSpot | null = null;
    const root = document.documentElement;
    root.classList.add("grabbing");
    const find = (x: number, y: number): DropSpot | null => {
      const el = document.elementFromPoint(x, y)?.closest<HTMLElement>("[data-drop-key]");
      if (!el) return null;
      const s = { key: el.dataset.dropKey!, categoryId: el.dataset.dropCategory || null };
      return d.kind === "category" && !s.categoryId ? null : s;   // a category goes among categories only
    };
    const move = (ev: PointerEvent) => {
      if (!moving && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 4) return;
      moving = true;
      spot = find(ev.clientX, ev.clientY);
      setGhost({ d, x: ev.clientX, y: ev.clientY });
      setOver(spot?.key ?? null);
    };
    const finish = (commit: boolean) => {
      root.classList.remove("grabbing");
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("keydown", key);
      setGhost(null);
      setOver(null);
      if (moving) {
        dragged.current = true;   // the click that follows the release is not a click
        setTimeout(() => (dragged.current = false), 0);
        if (commit && spot) drop.current(d, spot);
      }
    };
    const up = () => finish(true);
    const key = (ev: KeyboardEvent) => ev.key === "Escape" && finish(false);
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("keydown", key);
  };

  const swallowClick = (e: ReactMouseEvent) => {
    if (dragged.current) {
      e.preventDefault();
      e.stopPropagation();
    }
  };
  return { ghost, over, start, swallowClick };
}

interface Props {
  library: Library;
  route: Route;
  companion: CompanionId;
  conn: Connection;
  hasSession: boolean;
  /** the reading in 進行中的學習, marked among a book's guides */
  sessionId?: string | null;
  bookmarks?: Bookmarks;
  /** the page (line) being read in the open book: its bookmark is shown as the one you are on */
  readingAt?: { id: string; n: number } | null;
  onOpenPage?: (materialId: string, n: number) => void;
  onOpenSession?: (sessionId: string) => void;
  onRemoveBookmark?: (materialId: string, n: number) => void;
  collapsed: boolean;
  onToggle: () => void;
  onNavigate: (r: Route) => void;
  onLibraryChange: () => Promise<unknown>;
  onOpenSettings: () => void;
}

const PEEK_OPEN_MS = 120;    // a pointer only passing over the rail does not open it
const PEEK_CLOSE_MS = 300;
const PEEK_EXIT_MS = 160;   // .sidebar.peek.leaving animation

export function Sidebar({ library, route, companion, conn, hasSession, sessionId = null, bookmarks = {}, readingAt = null, onOpenPage, onOpenSession, onRemoveBookmark, collapsed, onToggle, onNavigate, onLibraryChange, onOpenSettings }: Props) {
  const t = useT();
  const [folded, setFolded] = useStored<string[]>("mentor.foldedCategories", []);
  // books shown open: their bookmarks and guided readings listed under them
  const [openBooks, setOpenBooks] = useStored<string[]>("mentor.openBooks", []);
  // a book's 書籤 / 導讀 groups folded on their own ("<material id>:marks" / ":guides"); open unless folded
  const [foldedSecs, setFoldedSecs] = useStored<string[]>("mentor.foldedBookSections", []);
  const toggleSec = (key: string) => setFoldedSecs(foldedSecs.includes(key) ? foldedSecs.filter((x) => x !== key) : [...foldedSecs, key]);
  const toggleBook = (id: string) => setOpenBooks(openBooks.includes(id) ? openBooks.filter((x) => x !== id) : [...openBooks, id]);
  // the guided readings of every book, read again when the page, the library or the current reading changes
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const routeKey = route.page === "material" ? `material:${route.id}` : route.page;
  useEffect(() => {
    let live = true;
    api
      .sessions()
      .then((xs) => live && setSessions(xs))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [routeKey, library, sessionId]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const importInto = useRef<string | null>(null);
  // folded: hovering the rail floats the whole sidebar over the page (nothing moves); leaving folds it again
  const [peek, setPeek] = useState(false);
  const [leaving, setLeaving] = useState(false);   // the card is sliding away (its exit animation), then unmounts
  const peekBox = useRef<HTMLElement>(null);
  const peekTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const enterPeek = () => {
    clearTimeout(peekTimer.current);
    setLeaving(false);   // back over it while it slides away: it stays
    if (!peek) peekTimer.current = setTimeout(() => setPeek(true), PEEK_OPEN_MS);
  };
  const leavePeek = () => {
    clearTimeout(peekTimer.current);
    peekTimer.current = setTimeout(() => {
      // stay while something inside is in use: a name being typed, a menu open, a drag
      const box = peekBox.current;
      if (box && (box.contains(document.activeElement) && document.activeElement?.tagName === "INPUT" || box.querySelector(".menu, .dock-menu") || document.documentElement.classList.contains("grabbing"))) return leavePeek();
      setLeaving(true);
      peekTimer.current = setTimeout(() => {
        setPeek(false);
        setLeaving(false);
      }, PEEK_EXIT_MS);
    }, PEEK_CLOSE_MS);
  };
  useEffect(() => () => clearTimeout(peekTimer.current), []);
  useEffect(() => {
    if (!collapsed) {
      setPeek(false);
      setLeaving(false);
    }
  }, [collapsed]);

  const run = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
      await onLibraryChange();
    } catch (e) {
      setError(e instanceof RequestError ? e.body.detail : String(e));
    }
  };

  const addFiles = async (files: File[], categoryId: string | null) => {
    if (files.length === 0) return;
    setBusy(true);
    let last: Material | null = null;
    await run(async () => {
      last = await importFiles(files, categoryId, t as never, (id) => onNavigate({ page: "material", id }));
    });
    setBusy(false);
    if (categoryId) setFolded(folded.filter((id) => id !== categoryId));
    if (last) onNavigate({ page: "material", id: (last as Material).id });
  };

  const pickFiles = (categoryId: string | null) => {
    importInto.current = categoryId;
    fileInput.current?.click();
  };

  /** Move category `id` to where `target` is (after it when moving down, before it when moving up). */
  const moveCategory = (id: string, target: string) => {
    const ids = library.categories.map((c) => c.id);
    const from = ids.indexOf(id), to = ids.indexOf(target);
    if (from < 0 || to < 0 || from === to) return;
    ids.splice(from, 1);
    ids.splice(to, 0, id);
    run(() => api.reorderCategories(ids));
  };
  const nudgeCategory = (id: string, step: -1 | 1) => {
    const ids = library.categories.map((c) => c.id);
    const target = ids[ids.indexOf(id) + step];
    if (target) moveCategory(id, target);
  };

  const drag = useSidebarDrag((d, spot) => {
    if (d.kind === "category") {
      if (spot.categoryId && spot.categoryId !== d.id) moveCategory(d.id, spot.categoryId);
      return;
    }
    const m = library.materials.find((x) => x.id === d.id);
    if (m && (m.category_id ?? null) !== spot.categoryId) {
      run(() => api.updateMaterial(d.id, { category_id: spot.categoryId }));
      if (spot.categoryId) setFolded(folded.filter((id) => id !== spot.categoryId));
    }
  });

  const toggleFold = (id: string) => setFolded(folded.includes(id) ? folded.filter((x) => x !== id) : [...folded, id]);

  // Files dragged in from the desktop are imported into the category they are dropped on (HTML5 drop: the web view
  // hands the files to the page). Materials and categories move with the pointer drag above.
  const dropProps = (key: string, categoryId: string | null) => ({
    "data-drop-key": key,
    "data-drop-category": categoryId ?? "",
    onDragOver: (e: DragEvent) => {
      if (e.dataTransfer.types.includes("Files")) {
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        setDropTarget(key);
      }
    },
    onDragLeave: () => setDropTarget((t) => (t === key ? null : t)),
    onDrop: (e: DragEvent) => {
      e.preventDefault();
      setDropTarget(null);
      addFiles([...e.dataTransfer.files], categoryId);
    },
  });

  if (collapsed && !peek) {
    return (
      <aside className="sidebar rail" aria-label={t("nav.label")} onMouseEnter={enterPeek} onMouseLeave={() => clearTimeout(peekTimer.current)}>
        <button className="icon-btn" onClick={onToggle} aria-label={t("side.expand")} title={t("side.expand")}>
          <Icon.expand />
        </button>
        {NAV.filter((n) => n.page !== "learn" || hasSession).map((n) => (
          <button key={n.page} className={`icon-btn ${route.page === n.page ? "active" : ""}`} onClick={() => onNavigate({ page: n.page })} aria-label={t(n.label)} title={t(n.label)}>
            {n.icon}
          </button>
        ))}
        <span className="spacer" />
        <CompanionDock companion={companion} conn={conn} compact onOpenSettings={onOpenSettings} />
      </aside>
    );
  }

  const groups: { key: string; category: Category | null; items: Material[] }[] = [
    ...library.categories.map((c) => ({ key: c.id, category: c, items: library.materials.filter((m) => m.category_id === c.id) })),
    { key: UNSORTED, category: null, items: library.materials.filter((m) => !m.category_id) },
  ];

  const card = (
    <aside
      className={`sidebar ${peek ? "peek" : ""} ${leaving ? "leaving" : ""}`}
      aria-label={t("nav.label")}
      onClickCapture={drag.swallowClick}
      ref={peekBox}
      onMouseEnter={peek ? enterPeek : undefined}
      onMouseLeave={peek ? leavePeek : undefined}
    >
      <div className="side-head">
        <span className="brand">Mentor</span>
        {peek ? (
          // keep it open: the sidebar is pinned back into the layout
          <button className="icon-btn" onClick={onToggle} aria-label={t("side.expand")} title={t("side.pin")}>
            <Icon.expand />
          </button>
        ) : (
          <button className="icon-btn" onClick={onToggle} aria-label={t("side.collapse")} title={t("side.collapse")}>
            <Icon.collapse />
          </button>
        )}
      </div>

      <nav className="side-nav">
        {NAV.filter((n) => n.page !== "learn" || hasSession).map((n) => (
          <button key={n.page} className={`side-item ${route.page === n.page ? "active" : ""}`} onClick={() => onNavigate({ page: n.page })}>
            {n.icon}
            <span>{t(n.label)}</span>
          </button>
        ))}
      </nav>

      <div className="side-section">
        <span>{t("side.materials")}</span>
        <span className="side-actions always">
          <button className="icon-btn" onClick={() => setAdding(true)} aria-label={t("side.newCategory")} title={t("side.newCategory")}>
            <Icon.folderPlus />
          </button>
          <button className="icon-btn" onClick={() => pickFiles(null)} aria-label={t("side.import")} title={t("side.importHint")} disabled={busy}>
            <Icon.upload />
          </button>
        </span>
      </div>
      <input ref={fileInput} type="file" accept={ACCEPT} multiple hidden onChange={(e) => {
        addFiles([...(e.target.files ?? [])], importInto.current);
        e.target.value = "";
      }} />

      <div className="tree">
        {busy && <div className="side-note">{t("side.importing")}</div>}
        {error && (
          <div className="side-note bad" role="alert">
            {error}
          </div>
        )}
        {adding && (
          <NameInput
            placeholder={t("side.categoryName")}
            onDone={(name) => {
              setAdding(false);
              if (name) run(() => api.createCategory(name));
            }}
          />
        )}
        {groups.map(({ key, category, items }) => {
          // the unsorted group only shows up when it has something in it (or nothing exists yet)
          if (!category && items.length === 0 && library.categories.length > 0) return null;
          const open = !folded.includes(key);
          return (
            <div key={key} className={`group ${dropTarget === key || drag.over === key ? "drop" : ""}`} {...dropProps(key, category?.id ?? null)}>
              <CategoryRow
                name={category?.name ?? t("side.unsorted")}
                count={items.length}
                open={open}
                onToggle={() => toggleFold(key)}
                onImport={() => pickFiles(category?.id ?? null)}
                onRename={category ? (name) => run(() => api.renameCategory(category.id, name)) : undefined}
                onDelete={category ? () => run(() => api.deleteCategory(category.id)) : undefined}
                onMove={category ? (step) => nudgeCategory(category.id, step) : undefined}
                canMove={category ? [library.categories[0]?.id !== category.id, library.categories.at(-1)?.id !== category.id] : [false, false]}
                onDragStart={category ? drag.start({ kind: "category", id: category.id, label: category.name }) : undefined}
              />
              {open &&
                (items.length === 0 ? (
                  <div className="tree-empty">{library.materials.length === 0 ? t("side.emptyLibrary") : t("side.emptyCategory")}</div>
                ) : (
                  items.map((m) => {
                    const marks = bookmarks[m.id] ?? [];
                    const open = openBooks.includes(m.id);
                    return (
                      <div key={m.id} className={`sb-book ${open ? "open" : ""}`}>
                        <div className="sb-row">
                          {/* where it opens: the whole row is the toggle, this only shows which way it is */}
                          <span className="sb-chev" aria-hidden="true">
                            <span className={`chev ${open ? "open" : ""}`}>
                              <Icon.chevron />
                            </span>
                          </span>
                          <button
                            className={`side-item leaf ${route.page === "material" && route.id === m.id ? "active" : ""}`}
                            onPointerDown={drag.start({ kind: "material", id: m.id, label: m.title, tag: m.kind })}
                            onClick={() => {
                              // one click opens the book in the reader and opens (or folds) it here, as a Notion page
                              onNavigate({ page: "material", id: m.id });
                              toggleBook(m.id);
                            }}
                            aria-expanded={open}
                            title={m.filename}
                          >
                            <KindTag kind={m.kind} />
                            <span>{m.title}</span>
                          </button>
                        </div>
                        {open && (
                          <BookContents
                            material={m}
                            marks={marks}
                            guides={sessions.filter((x) => x.source?.material_id === m.id)}
                            sessionId={sessionId}
                            folded={(sec) => foldedSecs.includes(`${m.id}:${sec}`)}
                            onToggleSection={(sec) => toggleSec(`${m.id}:${sec}`)}
                            at={route.page === "material" && route.id === m.id && readingAt?.id === m.id ? readingAt.n : null}
                            onOpenPage={(n) => (onOpenPage ? onOpenPage(m.id, n) : onNavigate({ page: "material", id: m.id }))}
                            onOpenSession={(id) => onOpenSession?.(id)}
                            onRemoveBookmark={onRemoveBookmark ? (n) => onRemoveBookmark(m.id, n) : undefined}
                          />
                        )}
                      </div>
                    );
                  })
                ))}
            </div>
          );
        })}
      </div>

      {drag.ghost && (
        <div className="drag-ghost" style={{ left: drag.ghost.x + 14, top: drag.ghost.y + 10 }} aria-hidden="true">
          {drag.ghost.d.tag && <KindTag kind={drag.ghost.d.tag} />}
          <span>{drag.ghost.d.label}</span>
        </div>
      )}

      <div className="side-foot">
        <CompanionDock companion={companion} conn={conn} compact={false} onOpenSettings={onOpenSettings} />
      </div>
    </aside>
  );
  if (!peek) return card;
  // the rail keeps its place in the layout (one grid cell); the card floats over it and the page
  return <div className="sidebar rail peeking">{card}</div>;
}

const NAV: { page: "home" | "learn" | "history"; label: Key; icon: ReactNode }[] = [
  { page: "home", label: "nav.home", icon: <Icon.home /> },
  { page: "learn", label: "nav.learn", icon: <Icon.learn /> },
  { page: "history", label: "nav.history", icon: <Icon.history /> },
];

/** Under an open book, one step in (as Notion nests pages): the pages bookmarked in it, each with the chapter it falls
 * in, and its guided readings. The page you are on and the reading being learnt now are set bold. */
function BookContents({ material, marks, guides, sessionId, folded, onToggleSection, at, onOpenPage, onOpenSession, onRemoveBookmark }: {
  material: Material;
  marks: Bookmark[];
  guides: SessionListItem[];
  sessionId: string | null;
  /** whether a group is folded, and folding / opening it: a long list of bookmarks or guides is put away by its title */
  folded: (sec: "marks" | "guides") => boolean;
  onToggleSection: (sec: "marks" | "guides") => void;
  /** the page being read, when this book is open in the reader */
  at: number | null;
  onOpenPage: (n: number) => void;
  onOpenSession: (id: string) => void;
  onRemoveBookmark?: (n: number) => void;
}) {
  const t = useT();
  const where = (n: number) => t(material.unit === "page" ? "material.unit.pageNo" : "reader.atLine", { n });
  const ordered = [...guides].sort((a, b) => (a.source?.start ?? 0) - (b.source?.start ?? 0) || a.created_at.localeCompare(b.created_at));
  if (!marks.length && !ordered.length) return <div className="sb-empty">{t("side.bookEmpty")}</div>;
  return (
    <div className="sb-contents">
      {marks.length > 0 && (
        <div className="sb-sec" role="group" aria-label={t("side.bookmarks")}>
          <SectionToggle label={t("side.bookmarks")} count={marks.length} open={!folded("marks")} onToggle={() => onToggleSection("marks")} />
          {!folded("marks") && marks.map((b) => {
            const here = at === b.unit_no;
            return (
              <div key={b.unit_no} className="sb-item">
                <button className={`sb-line ${here ? "here" : ""}`} aria-current={here ? "location" : undefined} onClick={() => onOpenPage(b.unit_no)} title={b.chapter ? `${where(b.unit_no)} · ${b.chapter}` : where(b.unit_no)}>
                  <span className="sb-icon">
                    <Icon.bookmark filled={here} />
                  </span>
                  <span className="sb-text">{b.chapter ?? where(b.unit_no)}</span>
                  <span className="sb-n">{b.unit_no}</span>
                </button>
                {onRemoveBookmark && (
                  <button className="sb-x" onClick={() => onRemoveBookmark(b.unit_no)} aria-label={t("side.removeBookmark")} title={t("side.removeBookmark")}>
                    <Icon.close />
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
      {ordered.length > 0 && (
        <div className="sb-sec" role="group" aria-label={t("side.guides")}>
          <SectionToggle label={t("side.guides")} count={ordered.length} open={!folded("guides")} onToggle={() => onToggleSection("guides")} />
          {!folded("guides") && ordered.map((g) => {
            const now = g.id === sessionId;
            const meta = g.progress.stage === "quiz" ? t("side.guideQuiz") : `${g.progress.read}/${g.progress.total}`;
            return (
              <div key={g.id} className="sb-item">
                <button className={`sb-line ${now ? "here" : ""}`} onClick={() => onOpenSession(g.id)} title={now ? `${t("side.learningNow")} · ${g.source?.label ?? ""}` : g.source?.label}>
                  <span className={`sb-icon ${now ? "now" : ""}`} aria-label={now ? t("side.learningNow") : undefined}>
                    {now ? <Icon.learn /> : <Icon.book />}
                  </span>
                  <span className="sb-text">{g.source?.label ?? g.content_title}</span>
                  <span className="sb-n">{meta}</span>
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** A group's title under a book (書籤, 導讀): pressing it folds or opens the group, as a book's row does. */
function SectionToggle({ label, count, open, onToggle }: { label: string; count: number; open: boolean; onToggle: () => void }) {
  return (
    <button className={`sb-label ${open ? "open" : ""}`} onClick={onToggle} aria-expanded={open}>
      <span className="sb-icon" aria-hidden="true">
        <span className={`chev ${open ? "open" : ""}`}>
          <Icon.chevron />
        </span>
      </span>
      <span className="sb-text">{label}</span>
      <span className="sb-n">{count}</span>
    </button>
  );
}

function CategoryRow({ name, count, open, onToggle, onImport, onRename, onDelete, onMove, canMove, onDragStart }: {
  name: string;
  count: number;
  open: boolean;
  onToggle: () => void;
  onImport: () => void;
  onRename?: (name: string) => void;
  onDelete?: () => void;
  onMove?: (step: -1 | 1) => void;
  /** [can move up, can move down] */
  canMove: [boolean, boolean];
  /** a real category can be dragged to another place in the list */
  onDragStart?: (e: ReactPointerEvent) => void;
}) {
  const t = useT();
  const [menu, setMenu] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirming, setConfirming] = useState(false);
  useEffect(() => {
    if (!menu) setConfirming(false);
  }, [menu]);
  const row = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => !row.current?.contains(e.target as Node) && setMenu(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenu(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  if (renaming) {
    return (
      <NameInput
        initial={name}
        placeholder={t("side.categoryName")}
        onDone={(next) => {
          setRenaming(false);
          if (next && next !== name) onRename?.(next);
        }}
      />
    );
  }

  return (
    <div className="cat-row" ref={row}>
      <button
        className="side-item cat"
        onClick={onToggle}
        aria-expanded={open}
        onPointerDown={onDragStart}
      >
        <span className={`chev ${open ? "open" : ""}`}>
          <Icon.chevron />
        </span>
        <span>{name}</span>
        <span className="count">{count}</span>
      </button>
      <span className="side-actions">
        <button className="icon-btn" onClick={onImport} aria-label={t("side.importInto", { name })} title={t("side.importIntoHint")}>
          <Icon.plus />
        </button>
        {onRename && (
          <button className="icon-btn" onClick={() => setMenu((v) => !v)} aria-label={t("side.moreActions", { name })} aria-expanded={menu}>
            <Icon.more />
          </button>
        )}
      </span>
      {menu && (
        <div className="menu" role="menu">
          <button role="menuitem" onClick={() => { setMenu(false); setRenaming(true); }}>
            {t("side.rename")}
          </button>
          <button role="menuitem" disabled={!canMove[0]} onClick={() => { setMenu(false); onMove?.(-1); }}>
            {t("side.moveUp")}
          </button>
          <button role="menuitem" disabled={!canMove[1]} onClick={() => { setMenu(false); onMove?.(1); }}>
            {t("side.moveDown")}
          </button>
          <div className="menu-sep" role="separator" />
          {/* deleting asks once more, in place */}
          <button role="menuitem" className="danger" onClick={() => (confirming ? (setMenu(false), onDelete?.()) : setConfirming(true))}>
            {t(confirming ? "side.deleteConfirm" : "side.deleteCategory", { name })}
            <small>{t("side.deleteCategoryHint")}</small>
          </button>
        </div>
      )}
    </div>
  );
}

/** Inline text field: Enter or blur commits, Escape cancels (onDone receives "" when cancelled or empty). */
function NameInput({ initial = "", placeholder, onDone }: { initial?: string; placeholder: string; onDone: (name: string) => void }) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);
  const finish = (v: string) => {
    if (done.current) return;
    done.current = true;
    onDone(v.trim());
  };
  return (
    <input
      className="name-input"
      autoFocus
      aria-label={placeholder}
      placeholder={placeholder}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onFocus={(e) => e.target.select()}
      onBlur={() => finish(value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") finish(value);
        if (e.key === "Escape") finish("");
      }}
    />
  );
}
