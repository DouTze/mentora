import type { SessionView, Turn } from "../types";
import { useT } from "../i18n";
import { RichText } from "./RichText";
import { TutorBubble } from "./TutorBubble";

interface Props {
  turns: Turn[];
  showDetails?: boolean;
  /** the session's questions, so each turn can show the prompt the student was answering */
  questions?: SessionView["questions"];
}

export function TurnList({ turns, showDetails = false, questions = [] }: Props) {
  const t = useT();
  if (turns.length === 0) return <div className="muted">{t("turns.empty")}</div>;
  const prompts = Object.fromEntries(questions.map((q) => [q.id, q.prompt]));
  return (
    <div>
      {turns.map((turn, i) => (
        <div className="turn" key={turn.idx} data-testid="turn">
          <div className="who">
            {t("turns.header", {
              idx: turn.idx,
              phase: t(turn.phase === "diagnostic" ? "turns.phase.diagnostic" : "turns.phase.transfer"),
              question: turn.question_id,
            })}
          </div>
          {prompts[turn.question_id] && (i === 0 || turns[i - 1].question_id !== turn.question_id) && (
            <div className="turn-question" data-testid="turn-question">
              <strong>{t("turns.question")}</strong>
              <RichText text={prompts[turn.question_id]} inline />
            </div>
          )}
          <div className="you">
            <strong>{t("turns.you")}</strong>
            {turn.student_answer}
          </div>
          {turn.error ? (
            <div className="error">{t("turns.error", { error: turn.error })}</div>
          ) : (
            turn.tutor_message && <TutorBubble message={turn.tutor_message} />
          )}
          {showDetails && (
            <details>
              <summary>
                {t("turns.details", {
                  assessment: turn.judgement?.assessment ?? "—",
                  ok: String(turn.judgement?.reasoning_ok),
                  rule: turn.decision?.rule ?? "—",
                  action: turn.decision?.action ?? "—",
                  ms: turn.latency_ms ?? "?",
                })}
              </summary>
              <div className="muted">{t("turns.evidence", { evidence: turn.judgement?.evidence ?? "" })}</div>
              {turn.validation_notes && turn.validation_notes.length > 0 && (
                <div className="muted">{t("turns.validationNotes", { notes: turn.validation_notes.join(t("common.noteSeparator")) })}</div>
              )}
              <pre>{turn.model_raw}</pre>
              <div className="muted">
                {turn.backend} / {turn.model} · tokens {JSON.stringify(turn.usage)}
              </div>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
