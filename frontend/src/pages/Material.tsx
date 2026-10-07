import { useEffect, useMemo, useState } from "react";
import { api, RequestError } from "../api";
import type { Library, MaterialDetail, SessionListItem } from "../types";
import type { Route } from "../route";
import { Icon, KindTag } from "../components/Icons";
import { Select } from "../components/Select";
import { Working } from "../components/Working";
import { useLocale, useT } from "../i18n";


interface Props {
  id: string;
  library: Library;
  onLibraryChange: () => Promise<unknown>;
  onNavigate: (r: Route) => void;
  onOpenSession: (id: string) => void;
}

export function MaterialPage({ id, library, onLibraryChange, onNavigate, onOpenSession }: Props) {
  const t = useT();
  const locale = useLocale();
  const [material, setMaterial] = useState<MaterialDetail | null>(null);
  const [title, setTitle] = useState("");
  const [range, setRange] = useState<[number, number] | null>(null);
  const [view, setView] = useState<"text" | "original">("text");
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setMaterial(null);
    setRange(null);
    setError(null);
    setView("text");
    setConfirmDelete(false);
    api
      .material(id)
      .then((m) => {
        setMaterial(m);
        setTitle(m.title);
      })
      .catch((e: RequestError) => setError(e.body.detail));
    api
      .sessions()
      .then((all) => setSessions(all.filter((s) => s.source?.material_id === id)))
      .catch(() => {});
  }, [id]);

  const chars = useMemo(
    () => (material && range ? material.units.slice(range[0] - 1, range[1]).reduce((n, u) => n + u.length + 1, 0) : 0),
    [material, range],
  );

  if (!material) return <div className="doc">{error ? <div className="error">{error}</div> : <p className="muted">{t("material.loading")}</p>}</div>;

  const unitKey = material.unit === "page" ? "material.unit.page" : "material.unit.line";
  const unitName = t(unitKey);
  const selectedCount = range ? range[1] - range[0] + 1 : 0;
  const selectedUnitName = t(unitKey, { count: selectedCount });
  const total = material.unit_count;
  const category = library.categories.find((c) => c.id === material.category_id);
  const clamp = (n: number) => Math.min(total, Math.max(1, Math.round(n) || 1));

  // First click marks one unit; the next click on a later unit closes the range. Shift-click always extends.
  const pick = (n: number, shift: boolean) => {
    if (!range) return setRange([n, n]);
    if (shift) return setRange([Math.min(range[0], n), Math.max(range[1], n)]);
    if (range[0] === range[1] && n > range[0]) return setRange([range[0], n]);
    setRange([n, n]);
  };

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

  const startLearning = async () => {
    if (!range) return;
    setBusy(true);
    setError(null);
    try {
      const s = await api.startFromMaterial(material.id, range[0], range[1]);
      onOpenSession(s.id);
    } catch (e) {
      setError((e as RequestError).body.detail);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    await api.deleteMaterial(material.id);
    await onLibraryChange();
    onNavigate({ page: "home" });
  };

  // typed values are applied when the field is left (or Enter), so a half-typed end never drags the start along
  const commitRange = (a: number, b: number) => setRange([clamp(Math.min(a, b)), clamp(Math.max(a, b))]);

  return (
    <div className="doc wide">
      <div className="crumbs">
        <span>{category?.name ?? t("common.uncategorized")}</span>
        <span>/</span>
        <span>{material.title}</span>
      </div>
      <input
        className="doc-title"
        aria-label={t("material.name")}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onBlur={() => (title.trim() && title.trim() !== material.title ? patch({ title: title.trim() }) : setTitle(material.title))}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      />
      <div className="doc-meta">
        <KindTag kind={material.kind} />
        <span>{t("material.unitCount", { count: total, unit: t(unitKey, { count: total }) })}</span>
        <span>{material.filename}</span>
        <label>
          {t("material.category")}
          <Select
            value={material.category_id ?? ""}
            onChange={(v) => patch({ category_id: v || null })}
            options={[{ value: "", label: t("common.uncategorized") }, ...library.categories.map((c) => ({ value: c.id, label: c.name }))]}
          />
        </label>
        <span className="spacer" />
        {confirmDelete ? (
          <>
            <span>{t("material.delete.confirmText")}</span>
            <button className="danger" onClick={remove}>
              {t("material.delete.confirm")}
            </button>
            <button onClick={() => setConfirmDelete(false)}>{t("common.cancel")}</button>
          </>
        ) : (
          <button className="ghost" onClick={() => setConfirmDelete(true)}>
            <Icon.trash /> {t("material.delete")}
          </button>
        )}
      </div>

      <div className="workbench">
        <section className="reader" aria-label={t("material.content")}>
          {material.kind === "pdf" && (
            <div className="seg" role="tablist">
              <button role="tab" aria-selected={view === "text"} onClick={() => setView("text")}>
                {t("material.view.text")}
              </button>
              <button role="tab" aria-selected={view === "original"} onClick={() => setView("original")}>
                {t("material.view.original")}
              </button>
            </div>
          )}
          {view === "original" ? (
            <iframe className="pdf-frame" title={material.title} src={`${api.materialFileUrl(material.id)}#page=${range?.[0] ?? 1}`} />
          ) : (
            <div className={`units ${material.unit}`}>
              {material.units.map((text, i) => {
                const n = i + 1;
                const selected = !!range && n >= range[0] && n <= range[1];
                return (
                  <div
                    key={n}
                    className={`unit ${selected ? "selected" : ""} ${range?.[0] === n ? "first" : ""} ${range?.[1] === n ? "last" : ""}`}
                    data-testid="unit"
                    onClick={(e) => pick(n, e.shiftKey)}
                  >
                    <button className="unit-no" aria-label={t("material.unit.aria", { n, unit: unitName })} aria-pressed={selected}>
                      {material.unit === "page" ? t("material.unit.pageNo", { n }) : n}
                    </button>
                    <div className="unit-text">{text || (material.unit === "page" ? <span className="muted">{t("material.unit.emptyPage")}</span> : " ")}</div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <aside className="tool" aria-label={t("material.learn")}>
          <h2>
            <Icon.learn /> {t("material.learn")}
          </h2>
          <p className="muted">{t("material.learn.intro")}</p>

          <div className="range-inputs">
            <label>
              {locale === "en" ? t("material.range.startLabel", { unit: unitName }) : t("material.range.from")}
              <RangeInput value={range?.[0]} max={total} label={t("material.range.startLabel", { unit: unitName })} onCommit={(n) => commitRange(n, range?.[1] ?? n)} />
            </label>
            <label>
              {locale === "en" ? t("material.range.endLabel", { unit: unitName }) : t("material.range.to")}
              <RangeInput value={range?.[1]} max={total} label={t("material.range.endLabel", { unit: unitName })} onCommit={(n) => commitRange(range?.[0] ?? n, n)} />
              {locale === "zh-Hant" && unitName}
            </label>
          </div>
          <div className="row">
            <button className="ghost" onClick={() => setRange([1, total])}>
              {t("material.range.all")}
            </button>
            {range && (
              <button className="ghost" onClick={() => setRange(null)}>
                {t("material.range.clear")}
              </button>
            )}
          </div>

          <div className={`range-summary ${range ? "on" : ""}`} data-testid="range-summary">
            {range ? (
              <>
                <strong>{range[0] === range[1] ? t("material.range.single", { n: range[0], unit: unitName }) : t("material.range.span", { start: range[0], end: range[1], unit: selectedUnitName })}</strong>
                <span>{t("material.range.size", { count: selectedCount, unit: selectedUnitName, chars: chars.toLocaleString(locale) })}</span>
              </>
            ) : (
              <span>{t(material.unit === "page" ? "material.range.hint.page" : "material.range.hint.line")}</span>
            )}
          </div>

          {error && (
            <div className="error" role="alert">
              {error}
            </div>
          )}
          <button className="primary block" onClick={startLearning} disabled={!range || busy}>
            {t(busy ? "material.start.preparing" : "material.start")}
          </button>
          {busy && <Working label={t("material.start.working")} />}

          {sessions.length > 0 && (
            <>
              <h3>{t("material.sessions")}</h3>
              <ul className="mini-list">
                {sessions.map((s) => (
                  <li key={s.id}>
                    <button className="link" onClick={() => onOpenSession(s.id)}>
                      {s.source?.label}
                    </button>
                    <span className="muted">{s.created_at.slice(5, 10)}</span>
                    <span className={`badge ${s.status !== "completed" ? "" : s.verdict === "passed" ? "ok" : "warn"}`}>
                      {t(s.status !== "completed" ? "session.status.active" : s.verdict === "passed" ? "session.verdict.passed" : "session.verdict.review")}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}

/** number field that keeps what is typed until blur / Enter, then commits a clamped value */
function RangeInput({ value, max, label, onCommit }: { value: number | undefined; max: number; label: string; onCommit: (n: number) => void }) {
  const [text, setText] = useState(value === undefined ? "" : String(value));
  useEffect(() => setText(value === undefined ? "" : String(value)), [value]);
  const commit = () => {
    const n = Number(text);
    if (!text.trim() || !Number.isFinite(n)) return setText(value === undefined ? "" : String(value));
    onCommit(Math.min(max, Math.max(1, Math.round(n))));
  };
  return (
    <input
      type="number"
      min={1}
      max={max}
      value={text}
      placeholder="—"
      aria-label={label}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
    />
  );
}
