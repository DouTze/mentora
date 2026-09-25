import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LearnPage } from "./Learn";
import { hintMessage, mockFetch, session, turn } from "../test/fixtures";

const content = { json: { id: "correlation-causation", version: "0.1.0", title: "相關與因果", description: "", concepts: [{ id: "concept-01", title: "相關不等於因果", explanation: "E", examples: ["x"] }, { id: "concept-02", title: "共同原因", explanation: "E2", examples: [] }], questions: [] } };

afterEach(cleanup);

describe("LearnPage", () => {
  test("shows start panel and creates a session", async () => {
    const calls = mockFetch({
      "POST /api/sessions": () => ({ json: session() }),
    });
    let opened = "";
    render(<LearnPage sessionId={null} onSessionChange={(id) => (opened = id)} />);
    await userEvent.click(screen.getByRole("button", { name: "開始學習" }));
    await waitFor(() => expect(opened).toBe("abc123"));
    expect(calls[0].body).toEqual({ content_id: "correlation-causation", strategy_id: "rules-v1" });
  });

  test("submits an answer and shows the tutor hint", async () => {
    mockFetch({
      "GET /api/sessions/abc123": () => ({ json: session() }),
      "GET /api/content/correlation-causation": () => content,
      "POST /api/sessions/abc123/answer": (body) => ({
        json: {
          turn: turn((body as { answer: string }).answer, hintMessage),
          message: hintMessage,
          session: session({ current_question: { ...session().current_question!, hints_used: 1 }, turns: [turn("兩者一起發生", hintMessage)] }),
        },
      }),
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("question")).toHaveTextContent("冰淇淋");
    const box = screen.getByLabelText("你的回答") as HTMLTextAreaElement;
    await userEvent.type(box, "兩者一起發生");
    await userEvent.click(screen.getByRole("button", { name: "送出回答" }));
    const bubble = await screen.findByTestId("tutor-message");
    expect(bubble).toHaveTextContent("是否有第三個因素");
    expect(bubble).toHaveTextContent("教材出處：concept-01");
    expect(box.value).toBe(""); // cleared after success
    expect(screen.getByText(/提示 1\/2/)).toBeTruthy();
    expect(screen.queryAllByTestId("turn")).toHaveLength(0); // latest turn is the bubble above, not repeated
  });

  test("keeps the student's input and offers retry when the model is unavailable", async () => {
    mockFetch({
      "GET /api/sessions/abc123": () => ({ json: session() }),
      "GET /api/content/correlation-causation": () => content,
      "POST /api/sessions/abc123/answer": () => ({ status: 502, json: { code: "model_unavailable", detail: "Ollama 連線失敗", retryable: true, turn: null } }),
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await screen.findByTestId("question");
    const box = screen.getByLabelText("你的回答") as HTMLTextAreaElement;
    await userEvent.type(box, "我的答案");
    await userEvent.click(screen.getByRole("button", { name: "送出回答" }));
    const err = await screen.findByTestId("error");
    expect(err).toHaveTextContent("Ollama 連線失敗");
    expect(err).toHaveTextContent("你的回答已保留");
    expect(box.value).toBe("我的答案");
    expect(screen.getByRole("button", { name: "重試送出" })).toBeTruthy();
    expect(screen.queryByTestId("tutor-message")).toBeNull(); // no simulated fallback shown
  });

  test("renders the summary when the session is completed", async () => {
    mockFetch({
      "GET /api/sessions/abc123": () => ({
        json: session({
          status: "completed",
          current_question: null,
          summary: { practiced: [{ concept_id: "concept-01", title: "相關不等於因果", status: "passed" }, { concept_id: "concept-02", title: "共同原因", status: "needs_review" }], stuck_at: [{ question_id: "q-diag-02", concept_id: "concept-02", prompt: "鞋子尺寸題" }], review_next: [{ concept_id: "concept-02", title: "共同原因", explanation: "E2" }], turn_count: 6, error_count: 0, verdict: "needs_review" },
        }),
      }),
      "GET /api/content/correlation-causation": () => content,
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    const s = await screen.findByTestId("summary");
    expect(s).toHaveTextContent("有需要複習的概念");
    expect(s).toHaveTextContent("鞋子尺寸題");
    expect(screen.getByText("匯出 Markdown").getAttribute("href")).toBe("/api/sessions/abc123/export?format=md");
  });
});
