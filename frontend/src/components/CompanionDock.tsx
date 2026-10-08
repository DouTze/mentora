// The foot of the sidebar: the chosen 伴讀 companion and whether its model can be reached.
//
// The avatar's ring is the connection light — green connected, amber pulsing while connecting, red when the model
// cannot be reached, grey when no model is connected (the simulated backend, or before the first check). Beside it
// the companion says one short line; a line too long for the column first rests, then glides to its end. Pressing
// the dock opens a small menu: who the companion is, the connection in words (a local or a remote model), and the
// way into the settings.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { api } from "../api";
import { COMPANIONS, type CompanionId } from "../companions";
import { useLocale, useT, type Key } from "../i18n";
import type { ActiveBackend } from "../types";
import { Icon } from "./Icons";

export type ConnectionStatus = "idle" | "checking" | "ok" | "bad";

export interface Connection {
  status: ConnectionStatus;
  active: ActiveBackend | null;
  detail: string;
  /** when the last check finished (ms), null before the first */
  checkedAt?: number | null;
}

const RECHECK_MS = 5 * 60_000;
const FOCUS_RECHECK_MS = 2 * 60_000;

export type CheckResult = { active: ActiveBackend; health: { ok: boolean; detail?: string } };
const connectionOf = ({ active, health }: CheckResult): Connection => ({
  active, detail: health.detail ?? "", status: active.simulated ? "idle" : health.ok ? "ok" : "bad", checkedAt: Date.now(),
});

/** Asks the backend whether the current model answers: on start, every five minutes, on returning to the window after
 * two minutes away, and whenever `recheck` is called (after the settings change the model). A check made elsewhere
 * (the settings' 檢查連線) is handed in with `report`, so the dock shows the same answer at once. */
export function useConnection(): [Connection, () => void, (r: CheckResult) => void] {
  const [conn, setConn] = useState<Connection>({ status: "idle", active: null, detail: "" });
  const running = useRef(false);
  const last = useRef(0);
  const check = useCallback(() => {
    if (running.current) return;
    running.current = true;
    last.current = Date.now();
    setConn((c) => ({ ...c, status: "checking" }));
    api
      .checkSettings()
      .then((r) => setConn(connectionOf(r)))
      .catch((e) => setConn((c) => ({ ...c, status: "bad", detail: String(e?.body?.detail ?? e), checkedAt: Date.now() })))
      .finally(() => (running.current = false));
  }, []);
  useEffect(() => {
    check();
    const id = setInterval(check, RECHECK_MS);
    const onFocus = () => Date.now() - last.current > FOCUS_RECHECK_MS && check();
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [check]);
  const report = useCallback((r: CheckResult) => {
    last.current = Date.now();
    setConn(connectionOf(r));
  }, []);
  return [conn, check, report];
}

/** A model on this machine (OpenVINO, or Ollama on localhost) or one reached over the network. */
function where(active: ActiveBackend | null): "local" | "remote" {
  if (!active) return "local";
  if (active.backend === "remote") return "remote";
  if (active.backend === "ollama" && active.host && !/\/\/(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/.test(active.host)) return "remote";
  return "local";
}

const ENGINE: Record<string, string> = { ollama: "Ollama", openvino: "OpenVINO", remote: "Remote", mock: "Mock" };

/** A long host name kept readable: its start and its end. */
function shortHost(host?: string): string {
  if (!host) return "";
  let name = host;
  try {
    name = new URL(host).hostname;
  } catch {}
  // a tunnel's random sub-domain says nothing; its service does (…trycloudflare.com)
  return name.length > 22 && name.split(".").length > 2 ? `…${name.split(".").slice(-2).join(".")}` : name;
}

/** What went wrong, in words a reader can act on; the raw message stays in the tooltip. */
function reasonKey(detail: string): Key {
  if (/timeout|timed out|逾時/i.test(detail)) return "dock.reason.timeout";
  if (/無法連線|nodename|connect|refused|resolve|unreachable|name or service/i.test(detail)) return "dock.reason.unreachable";
  return "dock.reason.other";
}

function since(at: number, locale: string): string {
  const secs = Math.round((at - Date.now()) / 1000);
  const fmt = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  return Math.abs(secs) < 60 ? fmt.format(0, "second") : Math.abs(secs) < 3600 ? fmt.format(Math.round(secs / 60), "minute") : fmt.format(Math.round(secs / 3600), "hour");
}

const LINES: Key[] = ["dock.line.1", "dock.line.2", "dock.line.3", "dock.line.4", "dock.line.5", "dock.line.6", "dock.line.7", "dock.line.8"];
const LINE_MS = 90_000;

/** One line that rests, glides to its end when it does not fit, rests, and comes back. */
function Marquee({ text }: { text: string }) {
  const outer = useRef<HTMLSpanElement>(null);
  const inner = useRef<HTMLSpanElement>(null);
  const [shift, setShift] = useState(0);
  useLayoutEffect(() => {
    const measure = () => setShift(Math.max(0, (inner.current?.scrollWidth ?? 0) - (outer.current?.clientWidth ?? 0)));
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    if (outer.current) ro?.observe(outer.current);
    return () => ro?.disconnect();
  }, [text]);
  const moving = shift > 4;
  // slow enough to read while it moves: about 12px a second, with long rests at both ends
  const style = moving ? ({ "--shift": `-${shift}px`, animationDuration: `${14 + shift / 12}s` } as CSSProperties) : undefined;
  return (
    <span className={`marquee ${moving ? "moving" : ""}`} ref={outer}>
      <span key={text} className="marquee-text" ref={inner} style={style}>
        {text}
      </span>
    </span>
  );
}

function Avatar({ id, status, size }: { id: CompanionId; status: ConnectionStatus; size: number }) {
  const c = COMPANIONS.find((x) => x.id === id) ?? COMPANIONS[0];
  return (
    <span className={`dock-avatar ${status}`} style={{ width: size, height: size, "--tint": c.tint } as CSSProperties}>
      <img src={c.image} alt="" draggable={false} />
    </span>
  );
}

export function CompanionDock({ companion, conn, compact, onOpenSettings }: {
  companion: CompanionId;
  conn: Connection;
  /** the collapsed sidebar: the avatar alone */
  compact: boolean;
  onOpenSettings: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [line, setLine] = useState(() => Math.floor(Math.random() * LINES.length));
  const box = useRef<HTMLDivElement>(null);
  const name = t(`companion.${companion}.name` as Key);
  const locale = useLocale();
  // while the menu is open, "checked 2 minutes ago" keeps up with the clock
  const [, tick] = useState(0);
  useEffect(() => {
    if (!open) return;
    const id = setInterval(() => tick((n) => n + 1), 30_000);
    return () => clearInterval(id);
  }, [open]);

  useEffect(() => {
    const id = setInterval(() => setLine((n) => (n + 1 + Math.floor(Math.random() * (LINES.length - 1))) % LINES.length), LINE_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const place = t(where(conn.active) === "remote" ? "dock.remote" : "dock.local");
  const model = conn.active?.model ?? "";
  // the dock's line: the companion's small talk once connected, otherwise what is happening
  const said = conn.status === "ok" ? t(LINES[line]) : t(`dock.status.${conn.status}` as Key);
  const described =
    conn.status === "ok" ? t("dock.menu.ok", { where: place, model })
    : conn.status === "checking" ? t("dock.menu.checking", { where: place, model })
    : conn.status === "bad" ? t("dock.menu.bad", { where: place, model })
    : t("dock.menu.idle");

  return (
    <div className={`dock ${compact ? "compact" : ""}`} ref={box}>
      <button
        type="button"
        className="dock-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("dock.label", { name, status: described })}
        title={compact ? `${name} · ${described}` : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <Avatar id={companion} status={conn.status} size={compact ? 30 : 32} />
        {!compact && (
          <span className="dock-text">
            <span className="dock-name">{name}</span>
            <Marquee text={said} />
          </span>
        )}
      </button>
      {open && (
        <div className="dock-menu" role="menu">
          <div className="dock-menu-head">
            <Avatar id={companion} status={conn.status} size={44} />
            <span className="dock-menu-who">
              <span className="dock-menu-name">{name}</span>
              <span className="dock-menu-kind">{t(`companion.${companion}.creature` as Key)}</span>
            </span>
          </div>
          <div className="dock-facts">
            <div className={`dock-state ${conn.status}`}>
              <span className="dock-light" aria-hidden="true" />
              <span className="grow">{t(`dock.state.${conn.status}` as Key)}</span>
              {conn.checkedAt && conn.status !== "checking" && <span className="dock-checked">{t("dock.checked", { when: since(conn.checkedAt, locale) })}</span>}
            </div>
            <dl>
              <dt>{t("dock.fact.model")}</dt>
              <dd className="mono">{conn.status === "idle" && conn.active?.simulated ? t("dock.fact.noModel") : model || "—"}</dd>
              {conn.active && !conn.active.simulated && (
                <>
                  <dt>{t("dock.fact.source")}</dt>
                  <dd title={conn.active.host}>
                    {t(where(conn.active) === "remote" ? "dock.where.remote" : "dock.where.local")} · {where(conn.active) === "remote" ? shortHost(conn.active.host) || ENGINE[conn.active.backend] : ENGINE[conn.active.backend] ?? conn.active.backend}
                  </dd>
                </>
              )}
            </dl>
            {conn.status === "bad" && (
              <p className="dock-reason" title={conn.detail}>
                {t(reasonKey(conn.detail))}
              </p>
            )}
          </div>
          <div className="dock-menu-items">
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onOpenSettings();
              }}
            >
              <Icon.gear />
              {t("side.settings")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
