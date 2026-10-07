// Build the Python backend into one executable (PyInstaller, backend/mentor-backend.spec) and copy it to
// src-tauri/binaries/mentor-backend-<target triple>[.exe], the name Tauri's externalBin expects.
// PyInstaller does not cross-compile: run this on each OS you ship (macOS for the .app/.dmg, Windows for the .msi/.exe).
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "..");
const BACKEND = join(ROOT, "backend");
const WIN = process.platform === "win32";
const EXT = WIN ? ".exe" : "";

function run(cmd: string, args: string[], cwd: string) {
  const r = spawnSync(cmd, args, { cwd, stdio: "inherit" });
  if (r.status !== 0) process.exit(r.status ?? 1);
}

function targetTriple(): string {
  const r = spawnSync("rustc", ["--print", "host-tuple"], { encoding: "utf8" });
  if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
  const arch = process.arch === "arm64" ? "aarch64" : "x86_64";
  if (process.platform === "darwin") return `${arch}-apple-darwin`;
  if (WIN) return `${arch}-pc-windows-msvc`;
  return `${arch}-unknown-linux-gnu`;
}

if (!existsSync(join(BACKEND, ".venv"))) run("uv", ["venv"], BACKEND);
run("uv", ["pip", "install", "-e", ".[desktop]", "-c", "requirements.lock"], BACKEND);

const pyinstaller = join(BACKEND, WIN ? ".venv/Scripts/pyinstaller.exe" : ".venv/bin/pyinstaller");
run(pyinstaller, ["--noconfirm", "--clean", "mentor-backend.spec"], BACKEND);

const out = join(ROOT, "src-tauri", "binaries");
mkdirSync(out, { recursive: true });
const dest = join(out, `mentor-backend-${targetTriple()}${EXT}`);
copyFileSync(join(BACKEND, "dist", `mentor-backend${EXT}`), dest);
console.log(`sidecar → ${dest}`);
