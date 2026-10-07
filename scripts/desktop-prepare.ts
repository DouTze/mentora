// `tauri build` runs this first (beforeBuildCommand): a clean frontend build, then the Python sidecar.
import { spawnSync } from "node:child_process";
import { rmSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "..");
const FRONTEND = join(ROOT, "frontend");

function run(cmd: string, args: string[], cwd: string) {
  const r = spawnSync(cmd, args, { cwd, stdio: "inherit" });
  if (r.status !== 0) process.exit(r.status ?? 1);
}

// bun build adds new hashed files next to old ones; start from an empty dist so stale bundles are not shipped
rmSync(join(FRONTEND, "dist"), { recursive: true, force: true });
run("bun", ["install"], FRONTEND);
run("bun", ["run", "build"], FRONTEND);
run("bun", [join(import.meta.dir, "build-sidecar.ts")], ROOT);
