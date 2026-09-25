import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

describe("導讀 (ReadingPanel)", () => {
  test("fetches the guide of the current quote, lets the student ask, then moves on", async () => {
    let current = readingSession();
    const calls = mockFetch({
      "GET /api/sessions/abc123": () => ({ json: current }),
      "POST /api/sessions/abc123/reading/guide": () => {
        // the guide of whichever quote is current (the backend keeps the index)
        const rd = current.reading!;
        current = readingSession({ ...rd, guides: { ...rd.guides, [String(rd.index)]: guide0 } });
        return { json: { status: "ready", guide: guide0, session: current } };
      },
      "POST /api/sessions/abc123/reading/ask": (body) => {
        const q = (body as { question: string }).question;
        current = readingSession({ guides: { "0": guide0 }, qa: { "0": [{ question: q, answer: "葉綠體是進行光合作用的胞器。" }] } });
        return { json: { answer: { question: q, answer: "葉綠體是進行光合作用的胞器。" }, session: current } };
      },
      "POST /api/sessions/abc123/reading/next": () => {
        current = readingSession({ ...current.reading!, index: 1 });
        return { json: { status: "ready", session: current } };
      },
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("reading")).toHaveTextContent("導讀 1/2");
    expect(screen.getByTestId("reading-step")).toHaveTextContent("導讀 1/2");
    const guide = await screen.findByTestId("guide");
    expect(guide).toHaveTextContent("光合作用的原料與產物");
    expect(guide).toHaveTextContent("你能說出三個條件嗎？");
    expect(screen.queryByTestId("question")).toBeNull(); // no quiz question yet

    await userEvent.type(screen.getByLabelText("還有不懂的地方"), "葉綠體是什麼？");
    await userEvent.click(screen.getByRole("button", { name: "發問" }));
    expect(await screen.findByTestId("qa")).toHaveTextContent("葉綠體是進行光合作用的胞器");
    expect(calls.find((c) => c.key.endsWith("/reading/ask"))?.body).toEqual({ question: "葉綠體是什麼？" });

    await userEvent.click(screen.getByRole("button", { name: "我明白了，下一段" }));
    await waitFor(() => expect(screen.getByTestId("reading")).toHaveTextContent("導讀 2/2"));
    expect(screen.getByText("光反應")).toBeTruthy();
    // second (last) quote: its guide is requested and the button now offers to start the quiz
    await waitFor(() => expect(calls.filter((c) => c.key.endsWith("/reading/guide")).length).toBe(2));
    expect(await screen.findByRole("button", { name: "我明白了，開始出題" })).toBeTruthy();
  });

  test("shows the error and a retry when the guide cannot be produced", async () => {
    mockFetch({
      "GET /api/sessions/abc123": () => ({ json: readingSession() }),
      "POST /api/sessions/abc123/reading/guide": () => ({ status: 502, json: { code: "model_unavailable", detail: "Ollama 連線失敗", retryable: true } }),
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    const err = await screen.findByTestId("error");
    expect(err).toHaveTextContent("Ollama 連線失敗");
    expect(screen.getByRole("button", { name: "重試" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /我明白了/ })).toBeNull();
  });

  test("after the reading is done the quiz question is shown with the reading step marked complete", async () => {
    const s = readingSession({ index: 1, done: true, guides: { "0": guide0, "1": guide0 }, quiz_source: "full" });
    s.concepts = [{ id: "concept-01", title: "光合作用", explanation: "E", examples: [] }];
    s.state = { concept_order: ["concept-01"], concept_index: 0, phase: "diagnostic", concepts: { "concept-01": { status: "in_progress" } } };
    s.current_question = { id: "q-diag-01", concept_id: "concept-01", concept_title: "光合作用", type: "diagnostic", prompt: "請說明光合作用的原料。", hints_used: 0, hints_total: 2 };
    mockFetch({ "GET /api/sessions/abc123": () => ({ json: s }) });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("question")).toHaveTextContent("光合作用的原料");
    expect(screen.getByTestId("reading-step")).toHaveTextContent("導讀 2/2 完成");
    expect(screen.queryByTestId("reading")).toBeNull();
  });
});


describe("導讀 · 背景工作與提前出題", () => {
  test("shows the stage while a job runs, polls, and offers to write the questions early", async () => {
    let current = readingSession({ guides: { "0": guide0 } });
    let polls = 0;
    const calls = mockFetch({
      "GET /api/sessions/abc123": () => {
        polls++;
        return { json: current };
      },
      "POST /api/sessions/abc123/reading/finish": () => {
        current = readingSession({ guides: { "0": guide0 }, job: { stage: "generate", error: null } });
        return { json: { status: "pending", session: current } };
      },
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await screen.findByTestId("guide");
    await userEvent.click(screen.getByRole("button", { name: "先出題" }));
    expect(screen.getByTestId("confirm-finish")).toHaveTextContent("剩下 1 段不讀了");
    await userEvent.click(screen.getByRole("button", { name: "確定，開始出題" }));
    const job = await screen.findByTestId("job");
    expect(job).toHaveTextContent("正在依讀過的內容出題");
    expect(screen.queryByRole("button", { name: /我明白了/ })).toBeNull(); // no actions while the server works
    expect(calls.some((c) => c.key.endsWith("/reading/finish"))).toBe(true);
    expect(polls).toBeGreaterThanOrEqual(1); // the initial load; polling continues every 2 s (not awaited here)
  });

  test("a failed job shows its error with a retry", async () => {
    mockFetch({
      "GET /api/sessions/abc123": () => ({ json: readingSession({ guides: { "0": guide0 }, job: { stage: "compact", error: { code: "model_unavailable", detail: "Ollama 連線失敗" } } }) }),
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    const err = await screen.findByTestId("error");
    expect(err).toHaveTextContent("Ollama 連線失敗");
    expect(screen.getByRole("button", { name: "重試" })).toBeTruthy();
  });
});
