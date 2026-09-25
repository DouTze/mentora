// The session workspace the backend writes (backend/app/tutor/reading_agent.py → ReadingWorkspace):
//   <ws>/manifest.json      quotes (without text) + parts + material title / range
//   <ws>/quotes/NN.md       one file per quote (NN = index + 1)
//   <ws>/guides/NN.md       導讀 already produced (written back by the backend)
//   <ws>/parts/part-NN.md   精簡筆記 of a finished part
// The agent never receives the whole range in one prompt; it opens what it needs through tools.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

export interface QuoteMeta {
  id: string;
  index: number;
  label: string;
  heading: string | null;
  excerpt: string;
  chars: number;
  part: number;
}

export interface Manifest {
  session_id: string;
  material_title: string;
  range_label: string;
  quotes: QuoteMeta[];
  parts: { index: number; label: string; quote_from: number; quote_to: number; chars: number }[];
}

export class Workspace {
  readonly manifest: Manifest;

  constructor(readonly dir: string) {
    const p = join(dir, "manifest.json");
    if (!existsSync(p)) throw new Error(`workspace 缺少 manifest.json：${dir}`);
    this.manifest = JSON.parse(readFileSync(p, "utf8")) as Manifest;
  }

  private static nn(i: number) {
    return String(i + 1).padStart(2, "0");
  }

  quoteText(index: number): string {
    const q = this.manifest.quotes[index];
    if (!q) throw new Error(`沒有第 ${index + 1} 段（共 ${this.manifest.quotes.length} 段）`);
    const raw = readFileSync(join(this.dir, "quotes", `${Workspace.nn(index)}.md`), "utf8");
    return raw.replace(/^# .*\n\n?/, "").trim(); // drop the "# label · heading" line the backend adds
  }

  guide(index: number): string | null {
    const p = join(this.dir, "guides", `${Workspace.nn(index)}.md`);
    return existsSync(p) ? readFileSync(p, "utf8") : null;
  }

  compact(part: number): string | null {
    const p = join(this.dir, "parts", `part-${Workspace.nn(part)}.md`);
    return existsSync(p) ? readFileSync(p, "utf8") : null;
  }

  listCompacts(): number[] {
    const d = join(this.dir, "parts");
    if (!existsSync(d)) return [];
    return readdirSync(d)
      .map((f) => /^part-(\d+)\.md$/.exec(f)?.[1])
      .filter((x): x is string => !!x)
      .map((x) => Number(x) - 1)
      .sort((a, b) => a - b);
  }
}
