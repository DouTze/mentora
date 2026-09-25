// Runs one task with a pi Agent: system prompt + tools + the task prompt, then extracts the JSON answer.
import { Agent } from "@earendil-works/pi-agent-core";
import { ollamaModels } from "./model";
import { askTask, compactTask, guideTask, segmentTask, SYSTEM, type QAHistory } from "./prompts";
import { makeTools } from "./tools";
import { Workspace } from "./workspace";

export interface Task {
  task: "guide" | "ask" | "compact" | "segment";
  workspace?: string;
  material_title?: string;
  atoms?: { heading?: string | null; text: string }[];
  index?: number;
  part?: number;
  quote_to?: number;
  question?: string;
  history?: QAHistory[];
  prev_points?: string[];
  source_is_chinese?: boolean;
  model: string;
  host: string;
  temperature?: number;
  context_tokens?: number;
  max_turns?: number;
}

export interface RunResult {
  result: unknown;
  model_raw: string;
  usage: { input: number; output: number };
  tool_calls: { name: string; args: unknown }[];
  turns: number;
}

export function taskPrompt(ws: Workspace | null, t: Task): string {
  if (t.task === "segment") return segmentTask(t.material_title ?? "", t.atoms ?? []);
  if (!ws) throw new Error("這個任務需要 workspace");
  if (t.task === "guide") return guideTask(ws, t.index ?? 0, t.prev_points ?? [], t.source_is_chinese);
  if (t.task === "ask") return askTask(ws, t.index ?? 0, t.question ?? "", t.history ?? []);
  if (t.task === "compact") return compactTask(ws, t.part ?? 0, t.quote_to);
  throw new Error(`未知的任務：${(t as { task: string }).task}`);
}

export function extractJson(text: string): unknown {
  const cleaned = text.replace(/<think>[\s\S]*?<\/think>/g, "").replace(/```(?:json)?/g, "").trim();
  const m = /\{[\s\S]*\}/.exec(cleaned);
  if (!m) throw new Error("模型輸出不含 JSON 物件");
  return JSON.parse(m[0]);
}

export async function runTask(t: Task): Promise<RunResult> {
  // segmentation happens before the workspace exists (the quotes are its content), so it gets no tools
  const ws = t.workspace ? new Workspace(t.workspace) : null;
  const { models, model } = ollamaModels(t.host, t.model, t.context_tokens ?? 16384);
  const toolCalls: { name: string; args: unknown }[] = [];
  let turns = 0;
  const maxTurns = t.max_turns ?? 8;
  const usage = { input: 0, output: 0 };

  const agent = new Agent({
    initialState: { systemPrompt: SYSTEM, model, tools: ws ? makeTools(ws) : [] },
    streamFn: (m, ctx, opts) => models.streamSimple(m, ctx, { ...opts, temperature: t.temperature ?? 0.2 }),
    toolExecution: "sequential",
    // a small model can loop on tools; cap the turns so the backend's timeout is never the first defence
    finishTurn: async ({ message }) => {
      turns++;
      if (message.stopReason === "error" || message.stopReason === "aborted") return;
      return turns >= maxTurns ? { action: "end" } : undefined;
    },
  });
  agent.subscribe((ev) => {
    if (ev.type === "tool_execution_start") toolCalls.push({ name: ev.toolName, args: ev.args });
    if (ev.type === "message_end" && ev.message.role === "assistant") {
      const u = (ev.message as { usage?: { input?: number; output?: number } }).usage;
      usage.input += u?.input ?? 0;
      usage.output += u?.output ?? 0;
    }
  });

  await agent.prompt(taskPrompt(ws, t));

  const last = [...agent.state.messages].reverse().find((m) => m.role === "assistant") as
    | { content: ({ type: string; text?: string } | string)[] | string; stopReason?: string; errorMessage?: string }
    | undefined;
  if (!last) throw new Error("模型沒有回覆");
  if (last.stopReason === "error") throw new Error(`模型錯誤：${last.errorMessage ?? "unknown"}`);
  const raw = typeof last.content === "string" ? last.content : last.content.map((b) => (typeof b === "string" ? b : b.type === "text" ? b.text ?? "" : "")).join("");
  let result: unknown;
  try {
    result = extractJson(raw);
  } catch (e) {
    const blocks = typeof last.content === "string" ? "text" : last.content.map((b) => (typeof b === "string" ? "text" : b.type)).join(",");
    throw new Error(`${(e as Error).message}（stopReason=${last.stopReason ?? "?"}，turns=${turns}，工具呼叫 ${toolCalls.length} 次，內容區塊=${blocks}；輸出開頭：${raw.slice(0, 400).replace(/\n/g, " ")}）`);
  }
  return { result, model_raw: raw, usage, tool_calls: toolCalls, turns };
}
