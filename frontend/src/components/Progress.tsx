import type { SessionView } from "../types";
import { useT, type Key } from "../i18n";

export function Progress({ session, concepts }: { session: SessionView; concepts: Record<string, string> }) {
  const t = useT();
  const st = session.state;
  const rd = session.reading;
  return (
    <div className="progress">
      {rd && (
        <span className={`step ${rd.done ? "passed" : "current"}`} data-testid="reading-step">
          {rd.done ? t("progress.reading.done", { total: rd.total }) : t("progress.reading.current", { n: rd.index + 1, total: rd.total })}
        </span>
      )}
      {rd && !rd.done && <span className="step">{t("progress.quizAfterReading")}</span>}
      {st.concept_order.map((cid, i) => {
        const c = st.concepts[cid];
        const cls = i === st.concept_index && session.status === "active" ? "current" : c.status;
        return (
          <span key={cid} className={`step ${cls}`}>
            {concepts[cid] ?? cid} · {STATUS[c.status] ? t(STATUS[c.status]) : c.status}
          </span>
        );
      })}
    </div>
  );
}

const STATUS: Record<string, Key> = {
  not_started: "progress.status.not_started",
  in_progress: "progress.status.in_progress",
  passed: "progress.status.passed",
  passed_with_help: "progress.status.passed_with_help",
  needs_review: "progress.status.needs_review",
};
