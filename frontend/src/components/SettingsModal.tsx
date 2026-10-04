// Notion-style settings dialog: a left rail of sections (偏好, 伴讀) and the section's page on the right.
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { api, RequestError } from "../api";
import { COMPANIONS, seedCompanions, type CompanionId, type CompanionSettings } from "../companions";
import { LOCALES, useT, type Key, type Locale } from "../i18n";
import { usePrefs, type Theme } from "../prefs";
import type { ActiveBackend, ModelConfig, RemoteEndpoint } from "../types";
import { BackendBadge } from "./BackendBadge";
import { Icon } from "./Icons";

export type SettingsTab = "prefs" | "companion";

interface Props {
  initialTab?: SettingsTab;
  onClose: () => void;
  onBackendChanged: () => void;
}

export function SettingsModal({ initialTab = "prefs", onClose, onBackendChanged }: Props) {
  const t = useT();
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;

  // focus the dialog once on open and hand focus back to whatever opened it (the gear button) on close
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close.current();
    document.addEventListener("keydown", onKey);
    dialog.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, []);

  const tabs: { id: SettingsTab; label: Key; icon: ReactNode }[] = [
    { id: "prefs", label: "settings.tab.prefs", icon: <Icon.sliders /> },
    { id: "companion", label: "settings.tab.companion", icon: <Icon.companion /> },
  ];

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="settings" role="dialog" aria-modal="true" aria-label={t("settings.title")} ref={dialog} tabIndex={-1}>
        <nav className="settings-nav" role="tablist" aria-orientation="vertical">
          <div className="settings-nav-title">{t("settings.title")}</div>
          {tabs.map((x) => (
            <button key={x.id} role="tab" aria-selected={tab === x.id} className={`side-item ${tab === x.id ? "active" : ""}`} onClick={() => setTab(x.id)}>
              {x.icon}
              <span>{t(x.label)}</span>
            </button>
          ))}
        </nav>
        <section className="settings-body" role="tabpanel">
          <button className="icon-btn settings-close" onClick={onClose} aria-label={t("settings.close")} title={t("settings.close")}>
            <Icon.close />
          </button>
          {tab === "prefs" ? <PrefsPane /> : <CompanionPane onBackendChanged={onBackendChanged} />}
        </section>
      </div>
    </div>
  );
}

function PrefsPane() {
  const t = useT();
  const { theme, setTheme, locale, setLocale } = usePrefs();
  const themes: Theme[] = ["system", "light", "dark"];
  return (
    <>
      <h2 className="settings-h">{t("prefs.heading")}</h2>
      <div className="pref-row">
        <div>
          <div className="pref-label">{t("prefs.language")}</div>
          <div className="muted">{t("prefs.languageHint")}</div>
        </div>
        <select className="pref-control" aria-label={t("prefs.language")} value={locale} onChange={(e) => setLocale(e.target.value as Locale)}>
          {LOCALES.map((l) => (
            <option key={l.id} value={l.id}>
              {l.label}
            </option>
          ))}
        </select>
      </div>
      <div className="pref-row">
        <div>
          <div className="pref-label">{t("prefs.theme")}</div>
          <div className="muted">{t("prefs.themeHint")}</div>
        </div>
        <div className="theme-pick" role="radiogroup" aria-label={t("prefs.theme")}>
          {themes.map((id) => (
            <button key={id} role="radio" aria-checked={theme === id} className={theme === id ? "on" : ""} onClick={() => setTheme(id)}>
              <span className={`swatch swatch-${id}`} aria-hidden="true" />
              {t(`prefs.theme.${id}`)}
            </button>
          ))}
        </div>
      </div>
    </>
  );
}

type Health = { ok: boolean; detail: string; available_models?: string[]; [k: string]: unknown };

function CompanionPane({ onBackendChanged }: { onBackendChanged: () => void }) {
  const t = useT();
  const [saved, setSaved] = useState<CompanionSettings | null>(null);
  const [draft, setDraft] = useState<CompanionSettings | null>(null);
  const [active, setActive] = useState<ActiveBackend | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [remote, setRemote] = useState<RemoteEndpoint>({ url: "", has_token: false });
  const [remoteUrl, setRemoteUrl] = useState("");
  const [remoteToken, setRemoteToken] = useState("");

  useEffect(() => {
    Promise.all([api.companions(), api.settings()])
      .then(([c, s]) => {
        const settings = c ?? seedCompanions(s.config);
        setSaved(settings);
        setDraft(settings);
        setActive(s.active);
        if (s.remote) {
          setRemote(s.remote);
          setRemoteUrl(s.remote.url);
        }
      })
      .catch((e: RequestError) => setMsg({ ok: false, text: e.body.detail }));
    // the model picker suggests whatever the local Ollama already has pulled
    api.checkSettings().then((r) => setModels((r.health.available_models as string[] | undefined) ?? [])).catch(() => {});
  }, []);

  if (!draft || !saved) return <p className="muted">{msg?.text ?? t("companion.loading")}</p>;

  const id = draft.active;
  const cfg = draft.companions[id];
  const name = t(`companion.${id}.name`);
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const set = <K extends keyof ModelConfig>(k: K, v: ModelConfig[K]) =>
    setDraft({ ...draft, companions: { ...draft.companions, [id]: { ...cfg, [k]: v } } });
  const choose = (next: CompanionId) => {
    setDraft({ ...draft, active: next });
    setHealth(null);
    setMsg(null);
  };

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const r = await api.saveCompanions(draft);
      setSaved(r.companions);
      setDraft(r.companions);
      setActive(r.active);
      setHealth(null);
      setMsg({ ok: true, text: t("companion.saved") });
      onBackendChanged();
    } catch (e) {
      setMsg({ ok: false, text: (e as RequestError).body.detail });
    }
    setSaving(false);
  };

  const saveRemote = async () => {
    setMsg(null);
    try {
      const r = await api.saveRemote(remoteUrl.trim(), remoteToken.trim());
      setRemote(r.remote);
      setRemoteUrl(r.remote.url);
      setRemoteToken("");
      setHealth(null);
      setMsg({ ok: true, text: t("companion.remote.saved") });
    } catch (e) {
      setMsg({ ok: false, text: (e as RequestError).body.detail });
    }
  };
  const remoteDirty = remoteUrl.trim() !== remote.url || remoteToken.trim() !== "";

  // checks the saved backend, so it only makes sense once the draft is saved
  const check = async () => {
    setChecking(true);
    try {
      const r = await api.checkSettings();
      setHealth(r.health as Health);
      setModels((r.health.available_models as string[] | undefined) ?? models);
    } catch (e) {
      setHealth({ ok: false, detail: (e as RequestError).body.detail });
    }
    setChecking(false);
  };

  return (
    <>
      <h2 className="settings-h">{t("companion.heading")}</h2>
      <p className="muted settings-lead">{t("companion.lead")}</p>

      <h3 className="settings-sub">{t("companion.roles")}</h3>
      <div className="companions" role="radiogroup" aria-label={t("companion.roles")}>
        {COMPANIONS.map((c) => {
          const on = c.id === id;
          const cname = t(`companion.${c.id}.name`);
          return (
            <button
              key={c.id}
              role="radio"
              aria-checked={on}
              aria-label={t("companion.choose", { name: cname })}
              className={`companion ${on ? "on" : ""}`}
              style={{ "--tint": c.tint } as CSSProperties}
              onClick={() => choose(c.id)}
            >
              <span className="companion-art">
                <img src={c.image} alt="" draggable={false} />
                <span className="companion-tag">
                  {cname}
                  <small>{t(`companion.${c.id}.creature`)}</small>
                </span>
                {saved.active === c.id && <span className="companion-live">{t("companion.current")}</span>}
              </span>
              <span className="companion-meta">
                <span className="companion-tagline">{t(`companion.${c.id}.tagline`)}</span>
                <code>{draft.companions[c.id].backend === "mock" ? "mock" : draft.companions[c.id].model || "—"}</code>
              </span>
            </button>
          );
        })}
      </div>

      <h3 className="settings-sub">{t("companion.model")}</h3>
      <p className="muted">{t("companion.modelHint", { name })}</p>
      <div className="grid2">
        <label className="field">
          {t("companion.backend")}
          <select value={cfg.backend} onChange={(e) => set("backend", e.target.value as ModelConfig["backend"])}>
            <option value="mock">{t("companion.backend.mock")}</option>
            <option value="ollama">{t("companion.backend.ollama")}</option>
            <option value="remote">{t("companion.backend.remote")}</option>
            <option value="openvino">{t("companion.backend.openvino")}</option>
          </select>
        </label>
        {(cfg.backend === "ollama" || cfg.backend === "remote") && (
          <label className="field">
            {t("companion.modelName")}
            <input value={cfg.model} onChange={(e) => set("model", e.target.value)} placeholder="qwen3-vl:4b" list="ollama-models" />
            <datalist id="ollama-models">
              {models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </label>
        )}
        {cfg.backend === "openvino" && (
          <label className="field">
            {t("companion.modelPath")}
            <input value={cfg.model_path} onChange={(e) => set("model_path", e.target.value)} />
          </label>
        )}
      </div>
      {cfg.backend === "mock" && <p className="muted">{t("companion.mockHint")}</p>}
      {cfg.backend === "remote" && (
        <div className="remote-endpoint" data-testid="remote-endpoint">
          <h3 className="settings-sub">{t("companion.remote.heading")}</h3>
          <p className="muted">{t("companion.remote.hint")}</p>
          <div className="grid2">
            <label className="field">
              {t("companion.remote.url")}
              <input value={remoteUrl} onChange={(e) => setRemoteUrl(e.target.value)} placeholder="https://….trycloudflare.com" spellCheck={false} />
            </label>
            <label className="field">
              {t("companion.remote.token")}
              <input
                type="password"
                autoComplete="off"
                value={remoteToken}
                onChange={(e) => setRemoteToken(e.target.value)}
                placeholder={remote.has_token ? t("companion.remote.tokenSet") : ""}
              />
            </label>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button onClick={saveRemote} disabled={!remoteDirty || !remoteUrl.trim()}>
              {t("companion.remote.save")}
            </button>
          </div>
        </div>
      )}

      <details className="advanced">
        <summary>
          <span className="chev">
            <Icon.chevron />
          </span>
          {t("companion.advanced")}
          <span className="muted">{t("companion.advancedHint")}</span>
        </summary>
        <div className="grid2">
          {cfg.backend === "ollama" && (
            <label className="field">
              {t("companion.host")}
              <input value={cfg.host} onChange={(e) => set("host", e.target.value)} placeholder="http://127.0.0.1:11434" />
            </label>
          )}
          {cfg.backend === "openvino" && (
            <label className="field">
              {t("companion.device")}
              <input value={cfg.device} onChange={(e) => set("device", e.target.value)} placeholder="GPU" />
            </label>
          )}
          <label className="field">
            <span className="field-head">
              {t("companion.temperature")}
              <output>{cfg.temperature.toFixed(1)}</output>
            </span>
            <input type="range" min="0" max="2" step="0.1" value={cfg.temperature} onChange={(e) => set("temperature", Number(e.target.value))} />
            <small>{t("companion.temperatureHint")}</small>
          </label>
          <label className="field">
            {t("companion.maxTokens")}
            <input type="number" min="1" value={cfg.max_tokens} onChange={(e) => set("max_tokens", Number(e.target.value))} />
          </label>
          <label className="field">
            {t("companion.contextTokens")}
            <input type="number" min="512" step="512" value={cfg.context_tokens} onChange={(e) => set("context_tokens", Number(e.target.value))} />
          </label>
          {cfg.backend !== "mock" && (
            <label className="field">
              {t("companion.parallel")}
              <input
                type="number"
                min="1"
                max="8"
                value={cfg.parallel ?? 1}
                onChange={(e) => set("parallel", Math.min(8, Math.max(1, Math.round(Number(e.target.value) || 1))))}
              />
              <small>{t("companion.parallelHint")}</small>
            </label>
          )}
        </div>
      </details>

      <div className="settings-foot">
        <span className="muted">{t("companion.nowUsing")}</span>
        <BackendBadge active={active} />
        {health && <span className={`badge ${health.ok ? "ok" : "bad"}`}>{health.detail}</span>}
        <span className="spacer" />
        {dirty && <span className="muted">{t("companion.unsaved")}</span>}
        {dirty ? (
          <button onClick={() => setDraft(saved)}>{t("companion.discard")}</button>
        ) : (
          <button onClick={check} disabled={checking}>
            {checking ? t("companion.checking") : t("companion.check")}
          </button>
        )}
        <button className="primary" onClick={save} disabled={!dirty || saving}>
          {saving ? t("companion.saving") : t("companion.save")}
        </button>
      </div>
      {msg && (
        <p className={msg.ok ? "muted" : "error"} role={msg.ok ? "status" : "alert"}>
          {msg.text}
        </p>
      )}
    </>
  );
}
