import { useEffect, useState } from "react";
import { api, RequestError } from "../api";
import type { SessionListItem, SessionView } from "../types";
import { TurnList } from "../components/TurnList";
import { Progress } from "../components/Progress";
import { RichText } from "../components/RichText";
import { Icon } from "../components/Icons";
import { useT } from "../i18n";

export function HistoryPage({ onOpen }: { onOpen: (id: string) => void }) {
  const t = useT();
  const [list, setList] = useState<SessionListItem[]>([]);
  const [selected, setSelected] = useState<SessionView | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const reload = () => api.sessions().then(setList).catch((e: RequestError) => setMsg(e.body.detail));
  useEffect(() => {
    reload();
  }, []);

  const open = async (id: string) => {
    setSelected(await api.session(id));
  };

  const replay = async (id: string) => {
    setMsg(t("history.replaying"));
    try {
      const r = await api.replay(id);
      const vars = { replayed: r.replayed_answers, total: r.total_answers, id: r.session.id };
      setMsg(r.error ? t("history.replay.doneInterrupted", { ...vars, detail: r.error.detail }) : t("history.replay.done", vars));
      await reload();
      await open(r.session.id);
    } catch (e) {
      setMsg((e as RequestError).body.detail);
    }
  };

  const remove = async (id: string) => {
    setConfirmDelete(null);
    try {
      await api.deleteSession(id);
      if (selected?.id === id) setSelected(null);
      setMsg(t("history.delete.done"));
      await reload();
    } catch (e) {
      setMsg((e as RequestError).body.detail);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>{t("history.title")}</h2>
        {msg && <div className="muted">{msg}</div>}
        {list.length === 0 ? (
          <div className="muted">{t("history.empty")}</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th className="nowrap">{t("history.col.time")}</th>
                <th>{t("history.col.material")}</th>
                <th className="nowrap">{t("history.col.strategy")}</th>
                <th className="nowrap">{t("history.col.model")}</th>
                <th className="nowrap">{t("history.col.status")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.map((s) => (
                <tr key={s.id}>
                  <td className="muted nowrap">{s.created_at.replace("T", " ").slice(0, 16)}</td>
                  <td>
                    {s.content_title}{" "}
                    <span className="muted">{s.source ? `${s.source.material_title} · ${s.source.label}` : t("common.demoVersion", { version: s.content_version })}</span>
                  </td>
                  <td className="nowrap">{s.strategy_id}</td>
                  <td className="nowrap">{s.simulated ? <span className="badge sim">{t("history.simulated")}</span> : s.backend}</td>
                  <td className="nowrap">
                    {s.status === "completed" ? (
                      <span className={`badge ${s.verdict === "passed" ? "ok" : "warn"}`}>{t(s.verdict === "passed" ? "session.verdict.passed" : "session.verdict.review")}</span>
                    ) : (
                      <span className="badge">{t("session.status.active")}</span>
                    )}
                  </td>
                  <td>
                    <div className="history-actions">
                      <button className="sm" onClick={() => open(s.id)}>
                        {t("history.view")}
                      </button>
                      {s.status === "active" && (
                        <button className="sm" onClick={() => onOpen(s.id)}>
                          {t("common.continue")}
                        </button>
                      )}
                      <button className="sm ghost" onClick={() => replay(s.id)}>
                        {t("history.replay")}
                      </button>
                      {confirmDelete === s.id ? (
                        <>
                          <span className="muted">{t("history.delete.confirmText")}</span>
                          <button className="sm danger" onClick={() => remove(s.id)}>
                            {t("history.delete.confirm")}
                          </button>
                          <button className="sm" onClick={() => setConfirmDelete(null)}>
                            {t("common.cancel")}
                          </button>
                        </>
                      ) : (
                        <button className="sm ghost" onClick={() => setConfirmDelete(s.id)}>
                          <Icon.trash /> {t("history.delete")}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {selected && (
        <div className="panel">
          <div className="row">
            <h2 style={{ margin: 0 }}>{t("history.record", { id: selected.id })}</h2>
            <a href={api.exportUrl(selected.id, "md")} download>
              {t("common.exportMarkdown")}
            </a>
            <a href={api.exportUrl(selected.id, "json")} download>
              {t("common.exportJson")}
            </a>
          </div>
          <div className="muted">
            {selected.content_title} · {selected.source ? `${selected.source.material_title} ${selected.source.label}` : t("common.demoVersion", { version: selected.content_version })} · {selected.strategy_id} v{selected.strategy_version} · {selected.model_config_snapshot.backend} /{" "}
            {selected.model_config_snapshot.resolved_model ?? selected.model_config_snapshot.model} · temp {selected.model_config_snapshot.temperature} · ctx{" "}
            {selected.model_config_snapshot.context_tokens}
          </div>
          <div style={{ margin: "10px 0" }}>
            <Progress session={selected} concepts={Object.fromEntries(selected.concepts.map((c) => [c.id, c.title]))} />
          </div>
          {selected.summary && (
            <div className="tutor">
              <div className="label">{t("history.summary")}</div>
              <div>{t("history.summary.result", { verdict: selected.summary.verdict, turns: selected.summary.turn_count, errors: selected.summary.error_count })}</div>
              {selected.summary.review_next.length > 0 && <div>{t("history.summary.review", { items: selected.summary.review_next.map((r) => r.title).join(t("common.listSeparator")) })}</div>}
            </div>
          )}
          {selected.reading && (
            <>
              <h2 style={{ marginTop: 14 }}>
                {t("history.reading.title", { total: selected.reading.total })}
                {selected.reading.stopped_at != null && <span className="muted">{t("history.reading.stoppedEarly", { n: selected.reading.stopped_at + 1 })}</span>}
              </h2>
              <div className="reading-log" data-testid="reading-log">
                {selected.reading.quotes.map((q) => {
                  const qa = selected.reading!.qa[String(q.index)] ?? [];
                  const g = selected.reading!.guides[String(q.index)];
                  return (
                    <details key={q.id}>
                      <summary>
                        {q.index + 1}. {q.heading ?? q.label}
                        <span className="muted">
                          {" · "}
                          {q.label}
                          {!g && ` · ${t("history.reading.notGuided")}`}
                          {qa.length > 0 && ` · ${t("history.reading.asked", { count: qa.length })}`}
                        </span>
                      </summary>
                      {g && <RichText className="muted" text={g.guide} />}
                      {qa.map((x, i) => (
                        <div key={i} className="tutor">
                          <div className="label">{t("common.youAsked", { question: x.question })}</div>
                          <RichText text={x.answer} />
                        </div>
                      ))}
                    </details>
                  );
                })}
              </div>
            </>
          )}
          <h2 style={{ marginTop: 14 }}>{t("history.turns")}</h2>
          <TurnList turns={selected.turns} showDetails questions={selected.questions} />
        </div>
      )}
    </>
  );
}
