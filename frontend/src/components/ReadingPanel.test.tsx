import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { LearnPage } from "../pages/Learn";
import { mockFetch, session } from "../test/fixtures";
import type { ReadingView, SessionView } from "../types";

afterEach(cleanup);

const quotes = [
  { id: "quote-01", index: 0, label: "第 3 行", heading: "光合作用", excerpt: "植物利用光能 … 才能進行。", text: "植物利用光能把二氧化碳和水轉換成葡萄糖與氧氣。", chars: 24, part: 0 },
  { id: "quote-02", index: 1, label: "第 7 行", heading: "光反應", excerpt: "光反應發生在 … 無法進行。", text: "光反應發生在類囊體膜上。", chars: 12, part: 0 },
];
const guide0 = { chinese_text: null, guide: "這一段的重點是光合作用的原料與產物。\n\n留意三個條件。", key_points: ["原料與產物"], check: "你能說出三個條件嗎？" };

function readingSession(over: Partial<ReadingView> = {}): SessionView {
  const reading: ReadingView = { index: 0, total: 2, done: false, quotes, parts: [{ index: 0, label: "第 3 行 至 第 7 行", quote_from: 0, quote_to: 1, chars: 36 }], guides: {}, qa: {}, compacts: {}, quiz_source: null, ...over };
  return session({
    content_title: "光合作用 第 1–15 行",
    source: { material_id: "m1", material_title: "光合作用", unit: "line", start: 1, end: 15, label: "第 1–15 行", text: "…" },
    reading,
    concepts: [],
    current_question: null,
    state: { concept_order: [], concept_index: 0, phase: "reading", concepts: {} },
  });
}

describe("導讀 · 舊版流程的紀錄", () => {
  test("a record left by the removed old flow is shown read-only: no guide request, no actions", async () => {
    const current = readingSession({ guides: { "0": guide0 } });
    const calls = mockFetch({ "GET /api/sessions/abc123": () => ({ json: current }) });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} onOpenMaterial={() => {}} />);
    await screen.findByTestId("read-only");
    expect(screen.getByTestId("guide").textContent).toContain("原料與產物");
    expect(screen.queryByRole("button", { name: "我明白了，下一段" })).toBeNull();
    expect(screen.queryByLabelText("還有不懂的地方")).toBeNull();
    expect(calls.every((c) => c.key.startsWith("GET "))).toBe(true);
  });
});
