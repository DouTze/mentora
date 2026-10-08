import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { Icon } from "./Icons";

/**
 * Notices that come and go without blocking the page (a file that is already in the library). `notify()` can be
 * called from anywhere; <Toaster /> (once, in App) shows them at the top right, each for a few seconds — longer while
 * the pointer is on it — with an optional action.
 */
export type Toast = { id: number; message: string; tone?: "info" | "warn"; action?: { label: string; run: () => void } };

let next = 1;
const listeners = new Set<(t: Toast) => void>();

export function notify(t: Omit<Toast, "id">): void {
  const toast = { ...t, id: next++ };
  listeners.forEach((l) => l(toast));
}

const SHOW_MS = 6500;

export function Toaster() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  useEffect(() => {
    const add = (t: Toast) => setToasts((all) => [...all.slice(-3), t]);
    listeners.add(add);
    return () => {
      listeners.delete(add);
    };
  }, []);
  const dismiss = (id: number) => setToasts((all) => all.filter((t) => t.id !== id));
  return (
    <div className="toaster" role="status" aria-live="polite">
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} onDismiss={() => dismiss(t.id)} />
      ))}
    </div>
  );
}

function ToastCard({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const t = useT();
  const timer = useRef(0);
  const start = () => {
    clearTimeout(timer.current);
    timer.current = window.setTimeout(onDismiss, SHOW_MS);
  };
  useEffect(() => {
    start();
    return () => clearTimeout(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className={`toast ${toast.tone ?? "info"}`} onMouseEnter={() => clearTimeout(timer.current)} onMouseLeave={start}>
      <span className="toast-mark" aria-hidden="true" />
      <p>{toast.message}</p>
      {toast.action && (
        <button
          type="button"
          className="toast-action"
          onClick={() => {
            toast.action!.run();
            onDismiss();
          }}
        >
          {toast.action.label}
        </button>
      )}
      <button type="button" className="toast-close" aria-label={t("common.close")} title={t("common.close")} onClick={onDismiss}>
        <Icon.close />
      </button>
    </div>
  );
}
