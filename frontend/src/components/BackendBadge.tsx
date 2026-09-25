import type { ActiveBackend } from "../types";
import { useT } from "../i18n";

export function BackendBadge({ active }: { active: ActiveBackend | null | undefined }) {
  const t = useT();
  if (!active) return <span className="badge bad">{t("backend.disconnected")}</span>;
  if (active.simulated) return <span className="badge sim">{t("backend.simulated")}</span>;
  return (
    <span className="badge ok">
      {active.backend} · {active.model}
    </span>
  );
}
