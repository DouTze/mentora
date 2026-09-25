// A pi-ai API implementation for Ollama's native /api/chat.
//
// Why not the OpenAI-compatible /v1 endpoint pi ships with? Ollama's /v1 has no way to turn a Qwen3 model's
// thinking off, and qwen3-vl spends its whole token budget thinking before it answers (stopReason=length,
// no text). /api/chat takes `think: false`, so the reply is the answer. Non-streaming: one request, one
// message; the events pi expects are emitted from the finished reply.
import {
  calculateCost,
  createAssistantMessageEventStream,
  type AssistantMessage,
  type AssistantMessageEventStream,
  type Model,
  type ProviderStreams,
  type StreamOptions,
  type Tool,
  type ToolCall,
  type TranscriptContext,
} from "@earendil-works/pi-ai";
import { getCurrentSystemPrompt, resolveTranscript, resolveTranscriptTools } from "@earendil-works/pi-ai/utils/transcript";

export const OLLAMA_CHAT_API = "ollama-chat";

interface OllamaMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  tool_calls?: { function: { name: string; arguments: Record<string, unknown> } }[];
  tool_name?: string;
}

function textOf(content: string | { type: string; text?: string }[]): string {
  return typeof content === "string" ? content : content.map((b) => (b.type === "text" ? b.text ?? "" : "")).join("");
}

function toOllamaMessages(context: TranscriptContext): { messages: OllamaMessage[]; tools: Tool[] } {
  const ctx = resolveTranscript(context, false);
  const system = getCurrentSystemPrompt(ctx.messages);
  const tools = resolveTranscriptTools(ctx.messages, false).requestTools;
  const out: OllamaMessage[] = [];
  if (system) out.push({ role: "system", content: system });
  for (const m of ctx.messages) {
    if (m.role === "user") out.push({ role: "user", content: textOf(m.content) });
    else if (m.role === "assistant") {
      const text = m.content.filter((b) => b.type === "text").map((b) => (b as { text: string }).text).join("");
      const calls = m.content.filter((b): b is ToolCall => b.type === "toolCall");
      out.push({ role: "assistant", content: text, ...(calls.length ? { tool_calls: calls.map((c) => ({ function: { name: c.name, arguments: c.arguments } })) } : {}) });
    } else if (m.role === "toolResult") out.push({ role: "tool", content: textOf(m.content), tool_name: m.toolName });
    // system messages other than the resolved one are folded by resolveTranscript
  }
  return { messages: out, tools };
}

function emptyMessage(model: Model<any>): AssistantMessage {
  return {
    role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: "pending", timestamp: Date.now(),
  };
}

function stream(model: Model<any>, context: TranscriptContext, options?: StreamOptions): AssistantMessageEventStream {
  const s = createAssistantMessageEventStream();
  const output = emptyMessage(model);
  (async () => {
    try {
      const { messages, tools } = toOllamaMessages(context);
      const body = {
        model: model.id,
        messages,
        tools: tools.map((t) => ({ type: "function", function: { name: t.name, description: t.description, parameters: t.parameters } })),
        stream: false,
        think: false,
        options: {
          temperature: options?.temperature ?? 0.2,
          num_predict: options?.maxTokens ?? model.maxTokens,
          num_ctx: model.contextWindow,
          ...(options?.samplingParams ?? {}),
        },
      };
      s.push({ type: "start", partial: output });
      const res = await fetch(`${model.baseUrl}/api/chat`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal: options?.signal,
      });
      if (!res.ok) throw new Error(`Ollama ${res.status}: ${(await res.text()).slice(0, 300)}`);
      const data = (await res.json()) as {
        message?: { content?: string; thinking?: string; tool_calls?: { id?: string; function: { name: string; arguments: Record<string, unknown> } }[] };
        done_reason?: string; prompt_eval_count?: number; eval_count?: number;
      };
      const msg = data.message ?? {};
      if (msg.content) {
        const idx = output.content.length;
        output.content.push({ type: "text", text: "" });
        s.push({ type: "text_start", contentIndex: idx, partial: output });
        (output.content[idx] as { text: string }).text = msg.content;
        s.push({ type: "text_delta", contentIndex: idx, delta: msg.content, partial: output });
        s.push({ type: "text_end", contentIndex: idx, content: msg.content, partial: output });
      }
      for (const [i, tc] of (msg.tool_calls ?? []).entries()) {
        const call: ToolCall = { type: "toolCall", id: tc.id ?? `call_${Date.now().toString(36)}_${i}`, name: tc.function.name, arguments: (tc.function.arguments ?? {}) as ToolCall["arguments"] };
        const idx = output.content.length;
        output.content.push(call);
        s.push({ type: "toolcall_start", contentIndex: idx, partial: output });
        s.push({ type: "toolcall_end", contentIndex: idx, toolCall: call, partial: output });
      }
      output.usage.input = data.prompt_eval_count ?? 0;
      output.usage.output = data.eval_count ?? 0;
      output.usage.totalTokens = output.usage.input + output.usage.output;
      output.usage.cost = calculateCost(model, output.usage);
      output.stopReason = msg.tool_calls?.length ? "toolUse" : data.done_reason === "length" ? "length" : "stop";
      output.rawStopReason = data.done_reason;
      s.push({ type: "done", reason: output.stopReason, message: output });
      s.end();
    } catch (e) {
      output.stopReason = (options?.signal?.aborted ? "aborted" : "error") as AssistantMessage["stopReason"];
      output.errorMessage = e instanceof Error ? e.message : String(e);
      s.push({ type: "error", reason: output.stopReason as "error" | "aborted", error: output });
      s.end();
    }
  })();
  return s;
}

export function ollamaChatApi(): ProviderStreams {
  return { stream, streamSimple: (model, context, options) => stream(model, context, options) };
}
