import { useEffect, useRef, useState, type DragEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useT, type Key } from "../i18n";
import { api, RequestError } from "../api";
import type { Category, Library, Material } from "../types";
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
  collapsed: boolean;
  onToggle: () => void;
  onNavigate: (r: Route) => void;
  onLibraryChange: () => Promise<unknown>;
  onOpenSettings: () => void;
}

export function Sidebar({ library, route, companion, conn, hasSession, collapsed, onToggle, onNavigate, onLibraryChange, onOpenSettings }: Props) {
  const t = useT();
  const [folded, setFolded] = useStored<string[]>("mentor.foldedCategories", []);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const importInto = useRef<string | null>(null);

  const run = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
      await onLibraryChange();
    } catch (e) {
      setError(e instanceof RequestError ? e.body.detail : String(e));
    }
  };

  const importFiles = async (files: File[], categoryId: string | null) => {
    if (files.length === 0) return;
    setBusy(true);
    let last: Material | null = null;
    await run(async () => {
      for (const f of files) last = await api.importMaterial(f, categoryId);
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
      importFiles([...e.dataTransfer.files], categoryId);
    },
  });

  if (collapsed) {
    return (
      <aside className="sidebar rail" aria-label={t("nav.label")}>
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

  return (
    <aside className="sidebar" aria-label={t("nav.label")} onClickCapture={drag.swallowClick}>
      <div className="side-head">
        <span className="brand">Mentor</span>
        <button className="icon-btn" onClick={onToggle} aria-label={t("side.collapse")} title={t("side.collapse")}>
          <Icon.collapse />
        </button>
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
        importFiles([...(e.target.files ?? [])], importInto.current);
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
                  items.map((m) => (
                    <button
                      key={m.id}
                      className={`side-item leaf ${route.page === "material" && route.id === m.id ? "active" : ""}`}
                      onPointerDown={drag.start({ kind: "material", id: m.id, label: m.title, tag: m.kind })}
                      onClick={() => onNavigate({ page: "material", id: m.id })}
                      title={m.filename}
                    >
                      <KindTag kind={m.kind} />
                      <span>{m.title}</span>
                    </button>
                  ))
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
}

const NAV: { page: "home" | "learn" | "history"; label: Key; icon: ReactNode }[] = [
  { page: "home", label: "nav.home", icon: <Icon.home /> },
  { page: "learn", label: "nav.learn", icon: <Icon.learn /> },
  { page: "history", label: "nav.history", icon: <Icon.history /> },
];

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
