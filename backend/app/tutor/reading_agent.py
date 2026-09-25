"""The reading agent: produces 導讀 for one quote, answers questions about it, and compacts a part.

Two implementations behind one interface:
- InProcessReadingAgent: builds the prompt here and calls the active LLM backend (mock / ollama / openvino).
  Default; works offline with the mock backend, which is what the tests use.
- PiReadingAgent: hands the task to the pi-based agent in ../agent (bun). That agent gets the session
  workspace on disk and reads quotes / notes through tools instead of receiving everything in one prompt.
  Selected with MENTOR_READING_AGENT=pi.

Both work on the same on-disk workspace (see ReadingWorkspace) so the documents exist either way."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .. import config
from ..llm.base import LLMError
from ..llm.registry import Registry
from . import reading

GUIDE_MAX_TOKENS = 2048
GUIDE_CONTEXT_TOKENS = 8192
COMPACT_MAX_TOKENS = 2048
COMPACT_CONTEXT_TOKENS = 16384


class ReadingAgentError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code          # model_unavailable | invalid_model_output
        self.detail = detail


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
        (self.dir / "guides" / f"{q['index'] + 1:02d}.md").write_text("\n".join(lines), encoding="utf-8")

    def write_compact(self, part: dict, markdown: str) -> None:
        (self.dir / "parts" / f"part-{part['index'] + 1:02d}.md").write_text(markdown, encoding="utf-8")


class ReadingAgent:
    name = "inproc"

    def __init__(self, registry: Registry):
        self.registry = registry

    async def guide(self, ws: ReadingWorkspace, content: dict, quote: dict, prev_points: list[str]) -> dict:
        raise NotImplementedError

    async def ask(self, ws: ReadingWorkspace, content: dict, quote: dict, guide: dict | None,
                  history: list[dict], question: str) -> dict:
        raise NotImplementedError

    async def compact(self, ws: ReadingWorkspace, content: dict, part: dict) -> dict:
        raise NotImplementedError

    async def segment(self, material_title: str, atoms: list[dict]) -> reading.Segmentation:
        raise NotImplementedError


class InProcessReadingAgent(ReadingAgent):
    async def _call(self, messages: list[dict], model: type, *, task: str, meta: dict[str, Any],
                    max_tokens: int, context_tokens: int, check=None) -> tuple[Any, dict]:
        cfg, backend = self.registry.cfg, self.registry.backend
        last: Exception | None = None
        for attempt in range(2):
            try:
                r = await backend.generate(messages, temperature=0.0 if attempt else cfg.temperature,
                                           max_tokens=max(cfg.max_tokens, max_tokens),
                                           context_tokens=max(cfg.context_tokens, context_tokens),
                                           meta={"task": task, "json_schema": model.model_json_schema(), **meta})
            except LLMError as e:
                raise ReadingAgentError("model_unavailable", str(e)) from e
            try:
                parsed = reading.parse_json(r.text, model)
                if check:
                    check(parsed)
                return parsed, {"model_raw": r.text, "latency_ms": r.latency_ms, "usage": r.usage,
                                "backend": backend.name, "model": backend.model, "agent": self.name}
            except reading.InvalidReadingOutput as e:
                last = e
        raise ReadingAgentError("invalid_model_output", f"模型輸出格式無效：{last}")

    async def guide(self, ws, content, quote, prev_points):
        src = content["source"]
        msgs = reading.guide_messages(src["material_title"], src["label"], quote, len(content["quotes"]), prev_points)
        g, rec = await self._call(msgs, reading.Guide, task="guide", meta={"quote_text": quote["text"]},
                                  max_tokens=GUIDE_MAX_TOKENS, context_tokens=GUIDE_CONTEXT_TOKENS,
                                  check=lambda g: reading.check_guide(quote, g))
        out = {**g.model_dump(), **rec}
        ws.write_guide(quote, out)
        return out

    async def ask(self, ws, content, quote, guide, history, question):
        msgs = reading.ask_messages(content["source"]["material_title"], quote, guide, history, question)
        a, rec = await self._call(msgs, reading.Answer, task="ask", meta={"quote_text": quote["text"], "question": question},
                                  max_tokens=1024, context_tokens=GUIDE_CONTEXT_TOKENS)
        return {"question": question, **a.model_dump(), **rec}

    async def compact(self, ws, content, part):
        qs = content["quotes"][part["quote_from"]:part["quote_to"] + 1]
        msgs = reading.compact_messages(content["source"]["material_title"], part, qs)
        c, rec = await self._call(msgs, reading.Compact, task="compact", meta={"part_text": "\n\n".join(q["text"] for q in qs)},
                                  max_tokens=COMPACT_MAX_TOKENS, context_tokens=COMPACT_CONTEXT_TOKENS)
        md = reading.compact_markdown(part, c)
        ws.write_compact(part, md)
        return {**c.model_dump(), "markdown": md, **rec}


    async def segment(self, material_title, atoms):
        msgs = reading.segment_messages(material_title, atoms)
        seg, _ = await self._call(msgs, reading.Segmentation, task="segment", meta={"atoms": [g["text"] for g in atoms]},
                                  max_tokens=1024, context_tokens=COMPACT_CONTEXT_TOKENS,
                                  check=lambda seg: reading.apply_segmentation(atoms, seg))
        return seg


class PiReadingAgent(ReadingAgent):
    """Runs `bun run src/cli.ts` in ../agent with a JSON task on stdin; expects one JSON object on stdout.
    The agent talks to Ollama itself (through pi-ai's OpenAI-compatible provider), so it needs the ollama backend."""

    name = "pi"

    def __init__(self, registry: Registry, agent_dir: Path):
        super().__init__(registry)
        self.agent_dir = agent_dir

    async def _run(self, task: dict) -> dict:
        cfg = self.registry.cfg
        if cfg.backend != "ollama":
            raise ReadingAgentError("model_unavailable", "pi 閱讀 agent 只支援 ollama 後端，請到模型設定切換。")
        payload = {**task, "model": cfg.model or config.OLLAMA_MODEL, "host": cfg.host or config.OLLAMA_HOST,
                   "temperature": cfg.temperature, "context_tokens": max(cfg.context_tokens, COMPACT_CONTEXT_TOKENS)}
        t0 = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                "bun", "run", "src/cli.ts", cwd=str(self.agent_dir),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload, ensure_ascii=False).encode()), timeout=600)
        except FileNotFoundError as e:
            raise ReadingAgentError("model_unavailable", "找不到 bun，無法啟動 pi 閱讀 agent。") from e
        except asyncio.TimeoutError as e:
            raise ReadingAgentError("model_unavailable", "pi 閱讀 agent 逾時。") from e
        latency = int((time.perf_counter() - t0) * 1000)
        if proc.returncode != 0:
            raise ReadingAgentError("model_unavailable", f"pi 閱讀 agent 失敗：{err.decode(errors='replace')[-800:]}")
        try:
            data = json.loads(out.decode())
        except ValueError as e:
            raise ReadingAgentError("invalid_model_output", f"pi 閱讀 agent 輸出不是 JSON：{out[-300:]!r}") from e
        if data.get("error"):
            raise ReadingAgentError(data.get("code", "invalid_model_output"), data["error"])
        return {**data, "latency_ms": latency, "backend": "ollama", "model": payload["model"], "agent": self.name}

    async def guide(self, ws, content, quote, prev_points):
        data = await self._run({"task": "guide", "workspace": str(ws.dir), "index": quote["index"], "prev_points": prev_points,
                                "source_is_chinese": reading.is_chinese(quote["text"])})
        g = reading.Guide.model_validate(data["result"])
        try:
            reading.check_guide(quote, g)
        except reading.InvalidReadingOutput as e:
            raise ReadingAgentError("invalid_model_output", str(e)) from e
        out = {**g.model_dump(), **{k: v for k, v in data.items() if k != "result"}}
        ws.write_guide(quote, out)
        return out

    async def ask(self, ws, content, quote, guide, history, question):
        data = await self._run({"task": "ask", "workspace": str(ws.dir), "index": quote["index"], "question": question,
                                "history": history})
        a = reading.Answer.model_validate(data["result"])
        return {"question": question, **a.model_dump(), **{k: v for k, v in data.items() if k != "result"}}

    async def compact(self, ws, content, part):
        data = await self._run({"task": "compact", "workspace": str(ws.dir), "part": part["index"], "quote_to": part["quote_to"]})
        c = reading.Compact.model_validate(data["result"])
        md = reading.compact_markdown(part, c)
        ws.write_compact(part, md)
        return {**c.model_dump(), "markdown": md, **{k: v for k, v in data.items() if k != "result"}}


    async def segment(self, material_title, atoms):
        data = await self._run({"task": "segment", "material_title": material_title,
                                "atoms": [{"heading": g.get("heading"), "text": g["text"]} for g in atoms]})
        seg = reading.Segmentation.model_validate(data["result"])
        try:
            reading.apply_segmentation(atoms, seg)
        except reading.InvalidReadingOutput as e:
            raise ReadingAgentError("invalid_model_output", str(e)) from e
        return seg


def build_reading_agent(registry: Registry) -> ReadingAgent:
    if config.READING_AGENT == "pi":
        return PiReadingAgent(registry, config.PI_AGENT_DIR)
    return InProcessReadingAgent(registry)
