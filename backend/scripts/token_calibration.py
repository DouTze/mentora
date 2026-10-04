"""Opt-in: calibrate the token estimate (app/llm/context.py) against a local model's real prompt token counts.

    cd backend
    .venv/bin/python scripts/token_calibration.py --model qwen3-vl:4b [--cases zh-opportunity-cost,formula-where]

Renders the reading graph's actual prompts on the fixed fixtures (with the simulated backend, so no model output is
needed), sends each one to Ollama with num_predict=1 and reads prompt_eval_count. Prints estimate / actual per
prompt kind. Nothing is downloaded; nothing leaves the machine."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.llm.context import estimate_messages  # noqa: E402
from app.llm.mock import MockBackend  # noqa: E402
from app.llm.ollama import THINK_PREFILL, needs_think_prefill  # noqa: E402
from app.models import ModelConfig  # noqa: E402
from evals import reading_eval as ev  # noqa: E402


async def prompts_of(cases: list[str]) -> list[tuple[str, list[dict]]]:
    from app.reading.evaluation import run_case
    from scripts.eval_reading import CountingBackend, attach_unit_guides

    class Capture(MockBackend):
        def __init__(self):
            super().__init__()
            self.seen: list[tuple[str, list[dict]]] = []

        async def generate(self, messages, **kw):
            self.seen.append(((kw.get("meta") or {}).get("step") or (kw.get("meta") or {}).get("task"), messages))
            return await super().generate(messages, **kw)

    cap = Capture()
    manifest = ev.load_manifest()
    for case in ev.select_cases(manifest, cases, include_broken=False):
        await run_case(case, ModelConfig(backend="mock"), CountingBackend(cap), ["segment", "guide"], Path("/tmp"), ev=ev,
                       attach_unit_guides=attach_unit_guides)
    return cap.seen


async def actual(host: str, model: str, messages: list[dict]) -> int | None:
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
    if needs_think_prefill(model):
        msgs.append({"role": "assistant", "content": THINK_PREFILL})
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{host}/api/chat", json={"model": model, "messages": msgs, "stream": False, "think": False,
                                                   "options": {"num_predict": 1, "num_ctx": 16384, "temperature": 0}})
        return r.json().get("prompt_eval_count")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-vl:4b")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--cases", default="zh-opportunity-cost,en-gradient-descent,formula-where,code-block,list-tcp")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows = []
    for kind, msgs in await prompts_of(args.cases.split(",")):
        a = await actual(args.host, args.model, msgs)
        if a:
            rows.append({"kind": kind, "estimate": estimate_messages(msgs), "actual": a,
                         "chars": sum(len(m["content"]) for m in msgs)})
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r["estimate"] / r["actual"])
    summary = {k: {"n": len(v), "min": round(min(v), 2), "avg": round(sum(v) / len(v), 2), "max": round(max(v), 2)}
               for k, v in sorted(by.items())}
    allr = [r["estimate"] / r["actual"] for r in rows]
    summary["ALL"] = {"n": len(allr), "min": round(min(allr), 2), "avg": round(sum(allr) / len(allr), 2), "max": round(max(allr), 2)}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
