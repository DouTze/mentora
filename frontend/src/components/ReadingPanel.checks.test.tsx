import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { LearnPage } from "../pages/Learn";
import { session } from "../test/fixtures";
import type { ReadingView, SessionView, WorkflowView } from "../types";
import { Working } from "./Working";

// reading-graph-v2 (R7): a guide held by the source or body check shows a plain-language reason; the check record
// (with claim ids) stays folded; retry is offered only when retrying can help.

const quotes = [{ id: "quote-01", index: 0, unit_id: "u-1", label: "第 1 頁", heading: null, excerpt: "w …", text: "w ← w − η∇L", chars: 11, part: 0 }];
const wfBase: WorkflowView = { waiting: "retry", stage: null, active_command: null, error: null, failed_command_id: "start-abc123",
  last_failed: "start-abc123", failed_node: "guide", runner_active: true };

function held(error: { code: string; detail: string; retryable?: boolean }): SessionView {
  const reading: ReadingView = { index: 0, total: 1, done: false, quotes, parts: [{ index: 0, label: "p", quote_from: 0, quote_to: 0, chars: 11 }],
    guides: {}, qa: {}, compacts: {}, quiz_source: null, segmentation: { planner: "model", candidates: 1, notes: [], status: "done" },
    job: { stage: "guide", detail_stage: "guide", error } };
  return session({ source: { material_id: "m1", material_title: "GD", unit: "page", start: 1, end: 1, label: "第 1 頁", text: "…" },
    reading, concepts: [], current_question: null, state: { concept_order: [], concept_index: 0, phase: "reading", concepts: {} },
    workflow_runner: "langgraph", revision: 1, workflow: { ...wfBase, error } });
}

function serve(s: SessionView) {
  globalThis.fetch = (async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify(s), { status: 200, headers: { "content-type": "application/json" } })) as typeof fetch;
}

afterEach(cleanup);

describe("held guides", () => {
  test("missing symbol evidence: plain headline, folded check record, retry offered", async () => {
    serve(held({ code: "notation_unverified", detail: "PDF 文字層無法確認符號的字型（位置：第 1 頁）", retryable: true }));
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("error-headline")).toHaveTextContent("需要確認公式");
    expect(screen.getByText("檢查紀錄")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重試" })).toBeTruthy();
  });

  test("a unit too long for the model: no retry button, the way forward instead", async () => {
    serve(held({ code: "context_overflow", detail: "understand：輸入約 40000 tokens …", retryable: false }));
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("error-headline")).toHaveTextContent("這一段太長");
    expect(screen.queryByRole("button", { name: "重試" })).toBeNull();
    expect(screen.getByText(/縮小範圍重新開始/)).toBeTruthy();
  });
});

describe("published guides with unchecked spots", () => {
  function published(unverified?: NonNullable<ReadingView["guides"][string]["unverified"]>): SessionView {
    const s = held({ code: "x", detail: "" });
    const guide = { chinese_text: null, guide: "梯度下降沿負梯度更新參數。", key_points: [], check: "", unverified };
    return { ...s, reading: { ...s.reading!, guides: { "0": guide }, job: null }, workflow: { ...wfBase, waiting: "student", error: null } };
  }

  test("the guide is shown, with a folded list of what could not be checked", async () => {
    serve(published([{ kind: "coverage", source: "w ← w − η∇L", guide: "沿負梯度更新" },
                     { kind: "coverage", source: "η 是學習率", guide: "學習率" }]));
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("guide")).toHaveTextContent("梯度下降沿負梯度更新參數");
    expect(screen.getByTestId("unverified")).toHaveTextContent("這段導讀有 2 處沒能自動核對");
    expect(screen.getByTestId("unverified")).toHaveTextContent("原文「η 是學習率」在導讀中的寫法沒能自動確認一致");
  });

  test("no note when everything was checked (and for guides published before the field existed)", async () => {
    serve(published(undefined));
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await screen.findByTestId("guide");
    expect(screen.queryByTestId("unverified")).toBeNull();
  });
});

describe("working stages", () => {
  test("v2 stages read as what the tutor is checking", () => {
    render(<Working stage="guide" detail="understand" />);
    expect(screen.getByTestId("working")).toHaveTextContent("正在核對這一段原文說了什麼");
    cleanup();
    render(<Working stage="guide" detail="body_audit" />);
    expect(screen.getByTestId("working")).toHaveTextContent("正在檢查導讀是否完整忠實");
  });
});

describe("a topic guided one block at a time", () => {
  function topic(failedSecond: boolean): SessionView {
    const s = held({ code: "x", detail: "" });
    const blocks = [
      { index: 0, source: "梯度是函數增加最快的方向。", status: "accepted" as const, guide: "梯度指出函數上升最快的方向。" },
      failedSecond
        ? { index: 1, source: "沿負梯度更新參數可以降低損失。", status: "failed" as const, guide: "", attempt: 3 }
        : { index: 1, source: "沿負梯度更新參數可以降低損失。", heading: "更新規則", status: "accepted" as const, guide: "往負梯度走一小步，損失就會下降。" },
    ];
    const guide = { chinese_text: null, guide: "", key_points: [], check: "為什麼要往負梯度走？", mode: "blocks" as const,
                    topic_summary: "從梯度的意義談到更新規則。", blocks, partial: failedSecond };
    return { ...s, reading: { ...s.reading!, guides: { "0": guide }, job: null }, workflow: { ...wfBase, waiting: "student", error: null } };
  }

  test("source and guide alternate, the self-check comes after the last block", async () => {
    serve(topic(false));
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    const blocks = await screen.findAllByTestId("block");
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toHaveTextContent("原文・第 1/2 塊");
    expect(blocks[0]).toHaveTextContent("梯度指出函數上升最快的方向");
    expect(blocks[1]).toHaveTextContent("更新規則");
    expect(screen.getByTestId("topic-summary")).toHaveTextContent("從梯度的意義談到更新規則");
    expect(screen.getByTestId("topic")).toHaveTextContent("讀完想一想");
    expect(screen.queryByTestId("retry-blocks")).toBeNull();
  });

  test("a block that did not pass shows its source only, and can be regenerated", async () => {
    serve(topic(true));
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    const failed = await screen.findByTestId("block-failed");
    expect(failed).toHaveTextContent("沒有通過檢查");
    expect(failed).toHaveTextContent("共試了 3 次");
    expect(screen.getAllByTestId("block")[1]).toHaveTextContent("沿負梯度更新參數可以降低損失");
    expect(screen.getAllByTestId("block-guide")).toHaveLength(1);
    expect(screen.getByTestId("retry-blocks")).toHaveTextContent("重新產生沒通過的部分");
    expect(screen.getByRole("button", { name: /下一段|讀完了|出題/ })).toBeTruthy();   // the reader can move on
  });
});
