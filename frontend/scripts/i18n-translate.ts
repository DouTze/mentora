// Fills in src/i18n/en.ts from the zh-Hant source dictionary using the codex CLI.
// Only keys that en.ts is missing (or whose zh text changed since the last run) are sent, so re-running is cheap.
//   bun run i18n:translate            translate what's missing / stale
//   bun run i18n:translate --all      retranslate everything
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { zh } from "../src/i18n/zh-Hant";
import { en } from "../src/i18n/en";
import { enPlurals } from "../src/i18n/en.plurals";

type Dict = Record<string, string>;
const SRC = join(import.meta.dir, "../src/i18n");
const STAMP = join(SRC, "en.source.json"); // zh text each en entry was translated from

const source = zh as Dict;
const current = en as Dict;
const stamp: Dict = existsSync(STAMP) ? JSON.parse(readFileSync(STAMP, "utf8")) : {};
const all = process.argv.includes("--all");
const todo = Object.fromEntries(Object.entries(source).filter(([k, v]) => all || !(k in current) || stamp[k] !== v));

if (Object.keys(todo).length === 0) {
  console.log("en.ts is up to date");
  process.exit(0);
}
console.log(`translating ${Object.keys(todo).length} keys with codex…`);

const work = mkdtempSync(join(tmpdir(), "mentor-i18n-"));
const out = join(work, "en.json");
const prompt = `You are localizing the UI of "Mentor", an AI study app (students import PDFs/notes, a study companion guides reading 導讀, asks questions, gives hints, and tracks learning sessions).
Translate the Traditional Chinese UI strings in the JSON below into natural, concise American English. Use a warm, clear tone, sentence case for labels and buttons, no trailing period on short labels, and periods on complete explanatory sentences. Translate the intent in context rather than word for word.
Rules:
- Keep every key; output values only in English.
- Keep {placeholders} exactly as written.
- Keys ending in .before/.link/.after (or .from/.to, .open/.close) are fragments of one sentence around an inline element — translate them so they read correctly when concatenated (quote brackets 「」 become curly quotes “ ”; list separators 、 and ； become ", " and "; ").
- Preserve product names, paths, and code identifiers where needed: Markdown, JSON, Ollama, OpenVINO, REMOTE_JOBS, OLLAMA_NUM_PARALLEL. In file picker hints, use "PDF, Markdown, or text files". Prefer clear labels such as "Maximum response length (tokens)" over internal parameter names.
- Companion names (keys companion.*.name) are character names: romanize them nicely (墨羽 → Moyu, 小栗 → Kuri, 汐 → Shio); creatures: owl / red fox / sea otter.
- Use "guided reading" for the activity and "reading guide" for the generated explanation. Use "section" for a reading segment and "part" for a group of sections or a block within a guide.
- 伴讀 → "companion" (lowercase within sentences); 教材 → "materials" for the library as a whole, "document" for one imported file, or "source text" for its contents. Never use "Materials name" or "Delete materials" for a single document.
- 學習紀錄 → "Learning history" for the screen, "session" for one entry; 進行中的學習 → "Current session"; 閱讀區 → "reading collection"; 書櫃範圍 → "Book folders"; 示範模式 → "demo mode"; 示範教材 → "sample lesson".
- Use "practice" or "practice questions" for the question phase (e.g. "Start practice"), "Check your understanding" for diagnostic questions, and "Apply what you’ve learned" for transfer questions.
- Counted messages use the plural by default; singular overrides are maintained separately in en.plurals.ts. Bare unit labels material.unit.page and material.unit.line must stay singular ("page", "line"); their plural forms are also in en.plurals.ts. Avoid "(s)" and ambiguous noun fragments. Do not invent claims or change what an action does.
Write ONLY a JSON object mapping each key to its English string to the file ${out} (UTF-8), then stop. Do not modify any other file.

Existing English for context (revise to reflect any changed Chinese meaning while preserving terminology):
${JSON.stringify(Object.fromEntries(Object.keys(todo).filter((k) => k in current).map((k) => [k, current[k]])), null, 2)}

Traditional Chinese source:
${JSON.stringify(todo, null, 2)}`;

const proc = Bun.spawnSync(["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", "-C", work, "-"], {
  stdin: new TextEncoder().encode(prompt),
  stdout: "inherit",
  stderr: "inherit",
});
if (proc.exitCode !== 0 || !existsSync(out)) {
  console.error("codex did not produce a translation");
  process.exit(1);
}

const translated: Dict = JSON.parse(readFileSync(out, "utf8"));
const missing = Object.keys(todo).filter((k) => typeof translated[k] !== "string");
const broken = Object.keys(todo).filter((k) => {
  const need = (source[k].match(/\{\w+\}/g) ?? []).sort().join();
  return k in translated && (translated[k].match(/\{\w+\}/g) ?? []).sort().join() !== need;
});
if (missing.length || broken.length) {
  console.error("rejected translation", { missing, placeholderMismatch: broken });
  process.exit(1);
}

// keep en.ts in zh key order so diffs line up with the source files
const merged: Dict = {};
for (const k of Object.keys(source)) if (k in translated || k in current) merged[k] = translated[k] ?? current[k];
const body = Object.entries(merged).map(([k, v]) => `  ${JSON.stringify(k)}: ${JSON.stringify(v)},`).join("\n");
writeFileSync(
  join(SRC, "en.ts"),
  `// Generated by \`bun run i18n:translate\` (codex) from zh-Hant.ts. Hand edits are fine; the script only rewrites keys whose zh text changed.\nimport type { Dict } from "./index";\n\nexport const en: Dict = {\n${body}\n};\n`,
);
writeFileSync(STAMP, JSON.stringify(Object.fromEntries(Object.keys(merged).map((k) => [k, source[k]])), null, 2) + "\n");
console.log(`wrote ${Object.keys(translated).length} translations to src/i18n/en.ts`);
const pluralKeys = Object.keys(todo).filter((k) => k in enPlurals);
if (pluralKeys.length) console.log(`Review the corresponding forms in src/i18n/en.plurals.ts: ${pluralKeys.join(", ")}`);
