// Tools the reading agent can call. All read-only: the backend owns the files and writes results back
// after validating the agent's JSON answer.
import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type } from "@earendil-works/pi-ai";
import type { Workspace } from "./workspace";

const text = (t: string) => ({ content: [{ type: "text" as const, text: t }], details: undefined });

export function makeTools(ws: Workspace): AgentTool<any>[] {
  return [
    {
      name: "list_quotes",
      label: "列出段落",
      description: "列出這次閱讀範圍的所有段落（來源單元）：編號、位置、標題、字數，以及哪些已經有導讀、哪些部分已有精簡筆記。",
      parameters: Type.Object({}),
      execute: async () => {
        const m = ws.manifest;
        const lines = m.quotes.map(
          (q) => `${q.index + 1}. [${q.label}]${q.heading ? ` ${q.heading}` : ""} · ${q.chars} 字 · 第 ${q.part + 1} 部分${ws.guide(q.index) ? " · 已有導讀" : ""}`,
        );
        const parts = m.parts.map((p) => `第 ${p.index + 1} 部分：第 ${p.quote_from + 1}–${p.quote_to + 1} 段（${p.chars} 字）${ws.compact(p.index) ? " · 已有精簡筆記" : ""}`);
        return text(`教材：${m.material_title} ${m.range_label}\n\n段落：\n${lines.join("\n")}\n\n部分：\n${parts.join("\n")}`);
      },
    },
    {
      name: "read_quote",
      label: "讀原文段落",
      description: "讀取第 n 段的原文（n 從 1 開始）。",
      parameters: Type.Object({ n: Type.Integer({ minimum: 1, description: "段落編號，從 1 開始" }) }),
      execute: async (_id, p) => text(ws.quoteText((p as { n: number }).n - 1)),
    },
    {
      name: "read_guide",
      label: "讀已有導讀",
      description: "讀取第 n 段已經產生的導讀（若有）。用來承接前面幾段建立的概念，避免重複解釋。",
      parameters: Type.Object({ n: Type.Integer({ minimum: 1 }) }),
      execute: async (_id, p) => text(ws.guide((p as { n: number }).n - 1) ?? "（這一段還沒有導讀）"),
    },
    {
      name: "read_compact",
      label: "讀精簡筆記",
      description: "讀取第 k 部分的精簡筆記（若有）。長範圍在讀過的部分只保留筆記，不必重讀原文。",
      parameters: Type.Object({ k: Type.Integer({ minimum: 1, description: "部分編號，從 1 開始" }) }),
      execute: async (_id, p) => text(ws.compact((p as { k: number }).k - 1) ?? "（這一部分還沒有精簡筆記）"),
    },
  ];
}
