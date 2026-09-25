import { useEffect, useRef, useState } from "react";
import { api, RequestError } from "../api";
import type { Library, SessionListItem } from "../types";
import type { Route } from "../route";
import { Icon, KindTag } from "../components/Icons";
import { useT } from "../i18n";

interface Props {
  library: Library;
  onLibraryChange: () => Promise<unknown>;
  onNavigate: (r: Route) => void;
  onOpenSession: (id: string) => void;
}

export function HomePage({ library, onLibraryChange, onNavigate, onOpenSession }: Props) {
  const t = useT();
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.sessions().then(setSessions).catch(() => {});
  }, []);

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

  const active = sessions.filter((s) => s.status === "active").slice(0, 4);
  const recent = [...library.materials].reverse().slice(0, 6);
  const categoryName = (id: string | null) => library.categories.find((c) => c.id === id)?.name ?? t("common.uncategorized");

  return (
    <div className="doc">
      <h1 className="doc-title static">{t(library.materials.length === 0 ? "home.title.empty" : "home.title")}</h1>
      <p className="lead">{t("home.lead")}</p>

      <div
        className={`dropzone ${over ? "over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); importFiles([...e.dataTransfer.files]); }}
      >
        <Icon.upload />
        <div>
          <strong>{t(busy ? "home.drop.importing" : "home.drop.title")}</strong>
          <span className="muted">{t("home.drop.formats")}</span>
        </div>
        <button className="primary" onClick={() => input.current?.click()} disabled={busy}>
          {t("home.drop.choose")}
        </button>
        <input ref={input} type="file" accept=".pdf,.md,.markdown,.txt" multiple hidden onChange={(e) => { importFiles([...(e.target.files ?? [])]); e.target.value = ""; }} />
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}

      {active.length > 0 && (
        <section>
          <h2 className="section-title">{t("home.active")}</h2>
          <ul className="rows">
            {active.map((s) => (
              <li key={s.id}>
                <button onClick={() => onOpenSession(s.id)}>
                  <Icon.learn />
                  <span className="grow">{s.source ? `${s.source.material_title} · ${s.source.label}` : s.content_title}</span>
                  <span className="muted">{t("common.continue")}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {recent.length > 0 && (
        <section>
          <h2 className="section-title">{t("home.recent")}</h2>
          <ul className="rows">
            {recent.map((m) => (
              <li key={m.id}>
                <button onClick={() => onNavigate({ page: "material", id: m.id })}>
                  <KindTag kind={m.kind} />
                  <span className="grow">{m.title}</span>
                  <span className="muted">
                    {t(m.unit === "page" ? "home.recent.meta.page" : "home.recent.meta.line", { category: categoryName(m.category_id), count: m.unit_count })}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {library.materials.length === 0 && (
        <p className="muted">
          {t("home.empty.before")}
          <button className="link" onClick={() => onNavigate({ page: "learn" })}>
            {t("home.empty.link")}
          </button>
          {t("home.empty.after")}
        </p>
      )}
    </div>
  );
}
