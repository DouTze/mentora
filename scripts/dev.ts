// Start backend (FastAPI :8000) and frontend (bun :5173) together, plus Ollama if it is not already up.
// Ctrl-C stops both; a second Ctrl-C (or 3s timeout) force-kills whatever is left.
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "..");
const BACKEND = join(ROOT, "backend");
const FRONTEND = join(ROOT, "frontend");

function setup(cmd: string, cwd: string) {
  const r = spawnSync(cmd, { cwd, shell: true, stdio: "inherit" });
  if (r.status !== 0) process.exit(r.status ?? 1);
}
if (!existsSync(join(BACKEND, ".venv"))) setup('uv venv && uv pip install -e ".[dev]" -c requirements.lock', BACKEND);
if (!existsSync(join(FRONTEND, "node_modules"))) setup("bun install", FRONTEND);

// Ollama: reuse one that is already running (desktop app, tool-box, another terminal) and leave it alone on exit;
// only an instance started here is stopped here.
const OLLAMA_URL = (process.env.OLLAMA_HOST ?? "http://127.0.0.1:11434").replace(/^(?!https?:\/\/)/, "http://");

async function ollamaUp(): Promise<boolean> {
  try {
    return (await fetch(`${OLLAMA_URL}/api/version`, { signal: AbortSignal.timeout(1000) })).ok;
  } catch {
    return false;
  }
}

async function startOllama(): Promise<ChildProcess | null> {
  if (await ollamaUp()) {
    console.log(`ollama → 已在執行（${OLLAMA_URL}），直接沿用；結束時不會關閉它`);
    return null;
  }
  if (!Bun.which("ollama")) {
    console.log("ollama → 找不到 ollama 指令，略過（示範模式仍可使用）");
    return null;
  }
  // its request log is noisy; only errors go to the terminal
  const child = spawn("ollama", ["serve"], { stdio: ["ignore", "ignore", "inherit"], detached: true });
  // Ctrl-C while waiting: the main handlers are not installed yet, so don't leave it orphaned
  const abort = () => {
    child.kill("SIGKILL");
    process.exit(130);
  };
  process.once("SIGINT", abort);
  for (let i = 0; i < 20 && child.exitCode === null; i++) {
    if (await ollamaUp()) {
      process.off("SIGINT", abort);
      console.log(`ollama → 已啟動（${OLLAMA_URL}），結束時會一起關閉`);
      return child;
    }
    await Bun.sleep(500);
  }
  process.off("SIGINT", abort);
  console.log("ollama → 啟動失敗，略過（示範模式仍可使用）");
  if (child.exitCode === null) child.kill("SIGKILL");
  return null;
}

const ollama = await startOllama();

// backend stays on 127.0.0.1: LAN users reach it only through the frontend proxy (frontend/server.ts binds 0.0.0.0).
// detached → each child leads its own process group, so we can kill the whole tree
// (uvicorn --reload spawns workers) with kill(-pid).
const children: ChildProcess[] = [
  spawn(".venv/bin/uvicorn", ["app.main:app", "--reload", "--host", "127.0.0.1", "--port", "8000"], { cwd: BACKEND, stdio: "inherit", detached: true }),
  spawn("bun", ["--hot", "server.ts"], { cwd: FRONTEND, stdio: "inherit", detached: true }),
];

function killAll(signal: NodeJS.Signals) {
  for (const c of ollama ? [...children, ollama] : children) {
    if (c.pid === undefined) continue;
    try {
      process.kill(-c.pid, signal);
    } catch {
      // group already gone
    }
  }
}

let stopping = false;
function shutdown(code: number) {
  if (stopping) {
    killAll("SIGKILL");
    process.exit(code);
  }
  stopping = true;
  console.log("\n停止中…（再按一次 Ctrl-C 強制關閉）");
  killAll("SIGTERM");
  setTimeout(() => {
    killAll("SIGKILL");
    process.exit(code);
  }, 3000);
}

let alive = children.length;
for (const c of children) {
  c.on("exit", (code) => {
    alive--;
    if (alive === 0) process.exit(stopping ? 0 : (code ?? 1));
    // one side died on its own → take the other down too
    if (!stopping) shutdown(code ?? 1);
  });
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));
process.on("SIGHUP", () => shutdown(0));
