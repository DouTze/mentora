// stdin: one JSON Task (see agent.ts) → stdout: one JSON object {result, model_raw, usage, tool_calls, turns}
// or {error, code}. The backend (PiReadingAgent) is the only caller; exit code is 0 for both so the backend
// reads the message from the JSON, not from stderr.
import { runTask, type Task } from "./agent";

const input = await Bun.stdin.text();
let task: Task;
try {
  task = JSON.parse(input) as Task;
} catch (e) {
  console.log(JSON.stringify({ error: `stdin 不是 JSON：${e}`, code: "invalid_model_output" }));
  process.exit(0);
}
try {
  const r = await runTask(task);
  console.log(JSON.stringify(r));
} catch (e) {
  const msg = e instanceof Error ? e.message : String(e);
  const code = /fetch|ECONNREFUSED|connect|模型錯誤|timeout/i.test(msg) ? "model_unavailable" : "invalid_model_output";
  console.log(JSON.stringify({ error: msg, code }));
}
