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

export function Working({ stage, label }: { stage?: JobStage; label?: string }) {
  const t = useT();
  return (
    <div className="working" role="status" data-testid="working">
      <span className="working-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span>{label ?? t(stage ? STAGE_LABEL[stage] : "working.default")}</span>
    </div>
  );
}
