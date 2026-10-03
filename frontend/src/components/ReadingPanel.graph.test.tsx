import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LearnPage } from "../pages/Learn";
import { question1, session } from "../test/fixtures";
import type { CommandView, ReadingView, SessionView, WorkflowView } from "../types";
import { pendingCommand, timing } from "../workflow";

// LangGraph sessions: every action is POST /events with a command id, then GET /commands/{id} until finished.

const quotes = [
  { id: "quote-01", index: 0, unit_id: "u-1", label: "第 3 行", heading: "光合作用", excerpt: "植物 … 進行。", text: "植物利用光能。", chars: 7, part: 0 },
  { id: "quote-02", index: 1, unit_id: "u-2", label: "第 7 行", heading: "光反應", excerpt: "光反應 … 進行。", text: "光反應發生在類囊體膜上。", chars: 12, part: 0 },
];
const guide0 = { chinese_text: null, guide: "光合作用把光能變成化學能。", key_points: [], check: "" };
const idleWf: WorkflowView = { waiting: "student", stage: null, active_command: null, error: null, failed_command_id: null,
  last_failed: null, failed_node: null, runner_active: true };

function graphSession(rd: Partial<ReadingView> = {}, wf: Partial<WorkflowView> = {}, over: Partial<SessionView> = {}): SessionView {
  const reading: ReadingView = { index: 0, total: 2, done: false, quotes, parts: [{ index: 0, label: "p", quote_from: 0, quote_to: 1, chars: 19 }],
    guides: { "0": guide0 }, qa: {}, compacts: {}, quiz_source: null, segmentation: { planner: "model", candidates: 4, notes: [], status: "done" }, ...rd };
  return session({ source: { material_id: "m1", material_title: "光合作用", unit: "line", start: 1, end: 9, label: "第 1–9 行", text: "…" },
    reading, concepts: [], current_question: null, state: { concept_order: [], concept_index: 0, phase: "reading", concepts: {} },
    workflow_runner: "langgraph", revision: 3, workflow: { ...idleWf, ...wf }, ...over });
}

function cmd(id: string, type: CommandView["type"], status: CommandView["status"], over: Partial<CommandView> = {}): CommandView {
  return { command_id: id, type, status, stage: null, result: null, error: null, retryable: false, created_at: "", updated_at: "", ...over };
}

type Route = (body: any, url: string) => { status?: number; json?: unknown; network?: boolean };
function server(routes: Record<string, Route>) {
  const calls: { key: string; body: any }[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = url.split("?")[0];
    const key = `${init?.method ?? "GET"} ${path.replace(/\/commands\/[^/]+$/, "/commands/:id")}`;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ key, body });
    const h = routes[key];
    if (!h) return new Response(JSON.stringify({ code: "unmocked", detail: key }), { status: 500 });
    const r = h(body, path);
    if (r.network) throw new TypeError("Failed to fetch");
    return new Response(JSON.stringify(r.json), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  return calls;
}

beforeEach(() => {
  timing.pollMs = 5;
  timing.resendMs = 5;
  sessionStorage.clear();
});
afterEach(cleanup);

describe("LangGraph session (events API)", () => {
  test("next is one command: id, unit and revision sent; polled until done; a lost response is resent with the same id", async () => {
    let current = graphSession();
    let posts = 0;
    let polls = 0;
    const calls = server({
      "GET /api/sessions/abc123": () => ({ json: current }),
      "POST /api/sessions/abc123/events": () => {
        posts++;
        if (posts === 1) return { network: true };          // the request may have reached the server: resend
        return { status: 202, json: { ...cmd("x", "next", "queued"), session: current } };
      },
      "GET /api/sessions/abc123/commands/:id": () => {
        polls++;
        if (polls < 2) return { json: cmd("x", "next", "running", { stage: "draft" }) };
        current = graphSession({ index: 1, guides: { "0": guide0, "1": { ...guide0, guide: "光反應在類囊體膜上。" } } });
        return { json: cmd("x", "next", "completed") };
      },
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "我明白了，下一段" }));
    await waitFor(() => expect(screen.getByTestId("guide")).toHaveTextContent("光反應在類囊體膜上"));
    const sent = calls.filter((c) => c.key === "POST /api/sessions/abc123/events").map((c) => c.body);
    expect(sent).toHaveLength(2);
    expect(sent[0].command_id).toBe(sent[1].command_id);
    expect(sent[0]).toMatchObject({ type: "next", unit_id: "u-1", expected_revision: 3 });
    expect(calls.some((c) => c.key.startsWith("POST /api/sessions/abc123/reading"))).toBe(false);   // no legacy endpoint
    expect(pendingCommand("abc123")).toBeNull();
  });

  test("a stale event shows the refusal and the latest session", async () => {
    const current = graphSession();
    server({
      "GET /api/sessions/abc123": () => ({ json: current }),
      "POST /api/sessions/abc123/events": () => ({ status: 409, json: { code: "stale", detail: "學習進度已更新，請重新整理後再試。",
        session: graphSession({ index: 1, guides: { "0": guide0, "1": { ...guide0, guide: "另一個分頁已前進" } } }) } }),
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "我明白了，下一段" }));
    expect(await screen.findByTestId("error")).toHaveTextContent("學習進度已更新");
    expect(screen.getByTestId("guide")).toHaveTextContent("另一個分頁已前進");
    expect(pendingCommand("abc123")).toBeNull();
  });

  test("after a reload the unconfirmed command is looked up first and resent only if the server never got it", async () => {
    sessionStorage.setItem("mentor.pending.abc123", JSON.stringify({ sid: "abc123", command_id: "c-1", type: "ask", payload: { question: "ATP？" } }));
    let current = graphSession();
    const calls = server({
      "GET /api/sessions/abc123": () => ({ json: current }),
      "GET /api/sessions/abc123/commands/:id": (_b, url) => (url.endsWith("/c-1") && !calls.some((c) => c.key.startsWith("POST"))
        ? { status: 404, json: { detail: "找不到這個操作" } } : { json: cmd("c-1", "ask", "completed") }),
      "POST /api/sessions/abc123/events": (b) => {
        current = graphSession({ qa: { "0": [{ question: "ATP？", answer: "能量分子。" }] } });
        return { status: 202, json: { ...cmd(b.command_id, "ask", "queued"), session: current } };
      },
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("qa")).toHaveTextContent("能量分子");
    const posts = calls.filter((c) => c.key === "POST /api/sessions/abc123/events");
    expect(posts).toHaveLength(1);
    expect(posts[0].body.command_id).toBe("c-1");
  });

  test("a reload after the command already finished does not send it again", async () => {
    sessionStorage.setItem("mentor.pending.abc123", JSON.stringify({ sid: "abc123", command_id: "c-2", type: "next", payload: {} }));
    const calls = server({
      "GET /api/sessions/abc123": () => ({ json: graphSession({ index: 1, guides: { "0": guide0, "1": guide0 } }) }),
      "GET /api/sessions/abc123/commands/:id": () => ({ json: cmd("c-2", "next", "completed") }),
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await waitFor(() => expect(pendingCommand("abc123")).toBeNull());
    expect(calls.some((c) => c.key.startsWith("POST"))).toBe(false);
  });

  test("a failed question stays in the box and retry targets that command", async () => {
    let current = graphSession();
    const calls = server({
      "GET /api/sessions/abc123": () => ({ json: current }),
      "POST /api/sessions/abc123/events": (b) => {
        if (b.type === "ask") return { status: 200, json: { ...cmd(b.command_id, "ask", "failed", { retryable: true,
          error: { code: "model_unavailable", detail: "Ollama 連線失敗" } }), session: current } };
        current = graphSession({ qa: { "0": [{ question: "ATP？", answer: "能量分子。" }] } });
        return { status: 200, json: { ...cmd(b.command_id, "retry", "completed"), session: current } };
      },
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await userEvent.type(await screen.findByLabelText("還有不懂的地方"), "ATP？");
    await userEvent.click(screen.getByRole("button", { name: "發問" }));
    expect(await screen.findByTestId("error")).toHaveTextContent("Ollama 連線失敗");
    expect((screen.getByLabelText("還有不懂的地方") as HTMLInputElement).value).toBe("ATP？");
    await userEvent.click(screen.getByRole("button", { name: "重試" }));
    expect(await screen.findByTestId("qa")).toHaveTextContent("能量分子");
    const posts = calls.filter((c) => c.key === "POST /api/sessions/abc123/events").map((c) => c.body);
    expect(posts[1]).toMatchObject({ type: "retry", payload: { target_command_id: posts[0].command_id } });
  });

  test("blocking failure: the stage error is shown and retry sends the failed command id", async () => {
    let current = graphSession({ index: 1, guides: { "0": guide0 }, job: { stage: "guide", error: { code: "model_unavailable", detail: "模型沒有回應" } } },
      { waiting: "retry", failed_command_id: "n-1", failed_node: "guide", error: { code: "model_unavailable", detail: "模型沒有回應" } });
    const calls = server({
      "GET /api/sessions/abc123": () => ({ json: current }),
      "POST /api/sessions/abc123/events": (b) => {
        current = graphSession({ index: 1, guides: { "0": guide0, "1": { ...guide0, guide: "重試後的導讀" } } });
        return { status: 200, json: { ...cmd(b.command_id, "retry", "completed"), session: current } };
      },
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("error")).toHaveTextContent("模型沒有回應");
    await userEvent.click(screen.getByRole("button", { name: "重試" }));
    await waitFor(() => expect(screen.getByTestId("guide")).toHaveTextContent("重試後的導讀"));
    expect(calls.find((c) => c.key === "POST /api/sessions/abc123/events")?.body).toMatchObject({ type: "retry", payload: { target_command_id: "n-1" } });
  });

  test("the review stage of a running guide is shown in student words", async () => {
    server({ "GET /api/sessions/abc123": () => ({ json: graphSession({ index: 1, job: { stage: "guide", detail_stage: "review", error: null } },
      { waiting: null, active_command: cmd("n", "next", "running", { stage: "review" }) }) }) });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    expect(await screen.findByTestId("guide-loading")).toHaveTextContent("正在對照原文檢查導讀");
  });

  test("answers are commands too; the tutor message comes from the command result", async () => {
    let current = session({ workflow_runner: "langgraph", revision: 9, workflow: { ...idleWf, waiting: "answer" } });
    const calls = server({
      "GET /api/sessions/abc123": () => ({ json: current }),
      "POST /api/sessions/abc123/events": (b) => ({ status: 202, json: { ...cmd(b.command_id, "answer", "queued"), session: current } }),
      "GET /api/sessions/abc123/commands/:id": () => ({ json: cmd("a", "answer", "completed", {
        result: { message: { action: "hint", feedback: "方向對了，再想想第三個因素。", hint: "天氣？", probe: null, explanation: null, examples: [], source_ids: [] } } }) }),
    });
    render(<LearnPage sessionId="abc123" onSessionChange={() => {}} />);
    await userEvent.type(await screen.findByLabelText("你的回答"), "夏天兩者都多");
    await userEvent.click(screen.getByRole("button", { name: "送出回答" }));
    expect(await screen.findByText("方向對了，再想想第三個因素。")).toBeTruthy();
    expect(calls.find((c) => c.key === "POST /api/sessions/abc123/events")?.body).toMatchObject({
      type: "answer", payload: { answer: "夏天兩者都多" }, question_id: question1.id, expected_revision: 9 });
    expect(calls.some((c) => c.key === "POST /api/sessions/abc123/answer")).toBe(false);
  });
});
