// 伴讀 roster. Each companion is a character card with its own local model; the text lives in the i18n dictionaries.
import owl from "./assets/companions/owl.webp";
import fox from "./assets/companions/fox.webp";
import otter from "./assets/companions/otter.webp";
import type { ModelConfig } from "./types";

export type CompanionId = "owl" | "fox" | "otter";

export interface Companion {
  id: CompanionId;
  image: string;
  /** soft colour behind the creature, picked from its fur/feathers */
  tint: string;
  defaults: Pick<ModelConfig, "model" | "temperature">;
}

export const COMPANIONS: Companion[] = [
  { id: "owl", image: owl, tint: "#8a6a45", defaults: { model: "qwen3-vl:8b", temperature: 0.2 } },
  { id: "fox", image: fox, tint: "#d9730d", defaults: { model: "qwen3-vl:2b", temperature: 0.5 } },
  { id: "otter", image: otter, tint: "#5b5ea6", defaults: { model: "qwen3-vl:4b", temperature: 0.3 } },
];

export interface CompanionSettings {
  active: CompanionId;
  companions: Record<CompanionId, ModelConfig>;
}

/** First run: the current model settings become 墨羽's, the others start from the same backend with their own model. */
export function seedCompanions(current: ModelConfig): CompanionSettings {
  const companions = Object.fromEntries(
    COMPANIONS.map((c) => [c.id, c.id === "owl" ? current : { ...current, ...c.defaults }]),
  ) as Record<CompanionId, ModelConfig>;
  return { active: "owl", companions };
}
