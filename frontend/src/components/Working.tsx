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
