"""The on-disk mirror of a reading session: data/reading/<session>/ with quotes, guides and part notes as Markdown
files, one per unit, so a person (or a tool) can open exactly what it needs. The source of truth stays in the
database; this mirror is written as guides and notes are accepted."""
from __future__ import annotations

import json
from pathlib import Path


class ReadingWorkspace:
    """data/reading/<session>/: quotes/NN.md, guides/NN.md, parts/part-NN.md, manifest.json.
    One file per unit so an agent (or a person) can open exactly what it needs."""

    def __init__(self, root: Path, sid: str):
        self.dir = root / sid

    def init(self, content: dict) -> None:
        (self.dir / "quotes").mkdir(parents=True, exist_ok=True)
        (self.dir / "guides").mkdir(exist_ok=True)
        (self.dir / "parts").mkdir(exist_ok=True)
        src = content["source"]
        manifest = {"session_id": self.dir.name, "material_title": src["material_title"], "range_label": src["label"],
                    "quotes": [{k: v for k, v in q.items() if k != "text"} for q in content["quotes"]],
                    "parts": content["parts"]}
        (self.dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for q in content["quotes"]:
            head = f"# {q['label']}" + (f" · {q['heading']}" if q.get("heading") else "")
            (self.dir / "quotes" / f"{q['index'] + 1:02d}.md").write_text(f"{head}\n\n{q['text']}\n", encoding="utf-8")

    def clear_quotes(self) -> None:
        d = self.dir / "quotes"
        if d.exists():
            for f in d.glob("*.md"):
                f.unlink()

    def write_guide(self, q: dict, g: dict) -> None:
        lines = [f"# 導讀 · {q['label']}", ""]
        if g.get("chinese_text"):
            lines += ["> **對應原文：** " + q["label"], f"> **原文節錄：** \"{q['excerpt']}\"", ">", "> **中文課文**", ">",
                      *(f"> {ln}" for ln in g["chinese_text"].splitlines()), ""]
        lines += [g["guide"], ""]
        if g.get("key_points"):
            lines += ["**重點**"] + [f"- {p}" for p in g["key_points"]] + [""]
        (self.dir / "guides").mkdir(parents=True, exist_ok=True)   # the mirror may be missing (data moved / restored)
        (self.dir / "guides" / f"{q['index'] + 1:02d}.md").write_text("\n".join(lines), encoding="utf-8")

    def write_compact(self, part: dict, markdown: str) -> None:
        (self.dir / "parts").mkdir(parents=True, exist_ok=True)
        (self.dir / "parts" / f"part-{part['index'] + 1:02d}.md").write_text(markdown, encoding="utf-8")
