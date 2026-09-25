import type { SessionView, TutorMessage, Turn } from "../types";

export const question1 = {
  id: "q-diag-01",
  concept_id: "concept-01",
  concept_title: "相關不等於因果",
  type: "diagnostic" as const,
  prompt: "冰淇淋銷量越高溺水人數越多，你同意「吃冰淇淋會導致溺水」嗎？",
  hints_used: 0,
  hints_total: 2,
};

export function session(over: Partial<SessionView> = {}): SessionView {
  return {
    id: "abc123",
    status: "active",
    created_at: "2026-09-16T12:00:00+00:00",
    updated_at: "2026-09-16T12:00:00+00:00",
    content_id: "correlation-causation",
    content_version: "0.1.0",
    content_title: "相關與因果",
    strategy_id: "rules-v1",
    strategy_version: "0.1.0",
    source: null,
    reading: null,
    questions: [{ id: "q-diag-01", concept_id: "concept-01", type: "diagnostic", prompt: question1.prompt }],
    concepts: [
      { id: "concept-01", title: "相關不等於因果", explanation: "E", examples: ["x"] },
      { id: "concept-02", title: "共同原因", explanation: "E2", examples: [] },
    ],
    model_config_snapshot: { backend: "mock", model: "", host: "", model_path: "", device: "GPU", temperature: 0.2, max_tokens: 512, context_tokens: 8192, simulated: true, resolved_model: "keyword-rules" },
    state: { concept_order: ["concept-01", "concept-02"], concept_index: 0, phase: "diagnostic", concepts: { "concept-01": { status: "in_progress" }, "concept-02": { status: "not_started" } } },
    current_question: question1,
    turns: [],
    summary: null,
    ...over,
  };
}

export const hintMessage: TutorMessage = {
  action: "hint",
  feedback: "你已經找到兩者的關聯，還需要想想其他可能原因。",
  hint: "是否有第三個因素，同時影響這兩件事？",
  probe: null,
  explanation: null,
  examples: [],
  source_ids: ["concept-01"],
};

export function turn(answer: string, message: TutorMessage | null, over: Partial<Turn> = {}): Turn {
  return { idx: 1, created_at: "", concept_id: "concept-01", question_id: "q-diag-01", phase: "diagnostic", student_answer: answer, tutor_message: message, ...over };
}

/** Minimal fetch mock: route table keyed by "METHOD /api/path". */
export function mockFetch(routes: Record<string, (body?: unknown) => { status?: number; json: unknown }>) {
  const calls: { key: string; body: unknown }[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const key = `${init?.method ?? "GET"} ${url.split("?")[0]}`;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ key, body });
    const handler = routes[key];
    if (!handler) return new Response(JSON.stringify({ code: "unmocked", detail: `unmocked ${key}` }), { status: 500 });
    const r = handler(body);
    return new Response(JSON.stringify(r.json), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  return calls;
}
