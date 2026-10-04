// "The tutor is working" indicator: a stage label and three dots stepping at 2 fps (CSS steps(), no JS timers,
// no continuous repaint). Shown while a background job runs or a request is in flight.
import type { JobStage } from "../types";
import { useT, type Key } from "../i18n";

const STAGE_LABEL: Record<JobStage, Key> = {
  segment: "working.stage.segment",
  guide: "working.stage.guide",
  compact: "working.stage.compact",
  generate: "working.stage.generate",
};
// finer stages reported by LangGraph sessions; anything else falls back to the coarse stage
const DETAIL_LABEL: Record<string, Key> = {
  analyze: "working.stage.analyze",
  context: "working.stage.context",
  draft: "working.stage.draft",
  review: "working.stage.review",
  revise: "working.stage.revise",
  // reading-graph-v2: source check before teaching, body check before publishing, verified notes and questions
  symbols: "working.stage.symbols",
  understand: "working.stage.understand",
  source_audit: "working.stage.source_audit",
  revise_source: "working.stage.revise_source",
  plan_bridges: "working.stage.plan_bridges",
  body_audit: "working.stage.body_audit",
  verify: "working.stage.verify",
  revise_body: "working.stage.revise_body",
  notes: "working.stage.notes",
  notes_audit: "working.stage.notes_audit",
  notes_revise: "working.stage.notes_revise",
  quiz_generate: "working.stage.quiz_generate",
  quiz_audit: "working.stage.quiz_audit",
};

export function Working({ stage, label, detail }: { stage?: JobStage; label?: string; detail?: string | null }) {
  const t = useT();
  return (
    <div className="working" role="status" data-testid="working">
      <span className="working-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span>{label ?? t(detail && DETAIL_LABEL[detail] ? DETAIL_LABEL[detail] : stage ? STAGE_LABEL[stage] : "working.default")}</span>
    </div>
  );
}
