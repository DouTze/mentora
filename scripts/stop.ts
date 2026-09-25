// Kill whatever is still listening on the dev ports (leftovers from a closed terminal, etc).
import { spawnSync } from "node:child_process";

const PORTS = [8000, Number(process.env.PORT ?? 5173)];

function listeners(port: number): number[] {
  const r = spawnSync("lsof", ["-ti", `tcp:${port}`, "-sTCP:LISTEN"], { encoding: "utf8" });
  return r.stdout.split("\n").filter(Boolean).map(Number);
}

function kill(pids: number[], signal: NodeJS.Signals) {
  for (const pid of pids) {
    try {
      process.kill(pid, signal);
    } catch {
      // already gone
    }
  }
}

let found = false;
for (const port of PORTS) {
  const pids = listeners(port);
  if (pids.length === 0) {
    console.log(`:${port} 沒有在跑`);
    continue;
  }
  found = true;
  console.log(`:${port} → 關閉 pid ${pids.join(", ")}`);
  kill(pids, "SIGTERM");
}

if (found) {
  await Bun.sleep(1500);
  for (const port of PORTS) {
    const pids = listeners(port);
    if (pids.length === 0) continue;
    console.log(`:${port} 還活著 → 強制關閉 pid ${pids.join(", ")}`);
    kill(pids, "SIGKILL");
  }
}
