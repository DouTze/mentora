import type { TutorMessage } from "../types";
import { useT, type Key } from "../i18n";
import { RichText } from "./RichText";

const ACTION_LABEL: Record<TutorMessage["action"], Key> = {
  advance: "tutor.action.advance",
  pass: "tutor.action.pass",
  probe: "tutor.action.probe",
  hint: "tutor.action.hint",
  explain: "tutor.action.explain",
};

export function TutorBubble({ message }: { message: TutorMessage }) {
  const t = useT();
  return (
    <div className={`tutor ${message.action === "explain" ? "explain" : ""}`} data-testid="tutor-message">
      <div className="label">{t("tutor.label", { action: t(ACTION_LABEL[message.action]) })}</div>
      <RichText text={message.feedback} />
      {message.hint && (
        <div>
          <strong>{t("tutor.hint")}</strong>
          <RichText text={message.hint} inline />
        </div>
      )}
      {message.probe && (
        <div>
          <strong>{t("tutor.probe")}</strong>
          <RichText text={message.probe} inline />
        </div>
      )}
      {message.explanation && (
        <div style={{ marginTop: 8 }}>
          <strong>{t("tutor.explanation")}</strong>
          <RichText text={message.explanation} inline />
          {message.examples.length > 0 && (
            <ul>
              {message.examples.map((e, i) => (
                <li key={i}><RichText text={e} inline /></li>
              ))}
            </ul>
          )}
        </div>
      )}
      {message.source_ids.length > 0 && <div className="muted">{t("tutor.sources", { ids: message.source_ids.join(", ") })}</div>}
    </div>
  );
}
