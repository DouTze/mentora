"""Minimal real-model check of the LangChain bridge (P1): one schema-constrained task and one round of
schema-constrained context selection, through the same adapters the app uses. Exit 0 only when both pass.

    .venv/bin/python scripts/runtime_smoke.py --backend ollama --model qwen3-vl:4b
    .venv/bin/python scripts/runtime_smoke.py --backend openvino --model-path models/qwen3-1.7b-int4-ov --device CPU
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.llm.runtime import BackendPool, CallBudget, ListSink, ModelRuntime, ModelSnapshot  # noqa: E402
from app.llm.tasks import TaskContext, TaskError, ToolDispatcher, run_structured, select_context  # noqa: E402

SOURCE = {"u1": "均值定理要求函數在閉區間上連續、在開區間內可微。",
          "u2": "在這兩個條件下，至少存在一點 c，使 f'(c) 等於平均變化率。"}


class Points(BaseModel):
    conditions: list[str] = Field(min_length=1)
    conclusion: str = Field(min_length=2)


class Ids(BaseModel):
    unit_ids: list[str] = Field(default_factory=list, max_length=2)


async def read_source_units(unit_ids: list[str]) -> str:
    if any(u not in SOURCE for u in unit_ids):
        raise ValueError("超出本次學習範圍")
    return "\n".join(f"[{u}] {SOURCE[u]}" for u in unit_ids)


async def main(args) -> int:
    snap = ModelSnapshot(args.backend, args.model, args.host, args.model_path, args.device, 0.2, 512, 8192)
    rt = ModelRuntime(snap, BackendPool())
    ok = True
    sink = ListSink()
    ctx = TaskContext(rt, CallBudget.for_task("guide"), sink)
    t0 = time.perf_counter()
    try:
        p, _ = await run_structured(ctx, [HumanMessage(content=(
            "請讀下面這段教材，列出它的成立條件與結論。只輸出 JSON："
            '{"conditions":["…"],"conclusion":"…"}\n\n' + SOURCE["u1"] + SOURCE["u2"]))], Points,
            task="guide", node="smoke.structured", prompt_version="smoke-1", min_max_tokens=512)
        print("structured ✓", json.dumps(p.model_dump(), ensure_ascii=False))
    except TaskError as e:
        ok = False
        print("structured ✗", e.kind, e.detail)
    tool = StructuredTool.from_function(coroutine=read_source_units, name="read_source_units",
                                        description="讀取本次學習範圍內指定的來源單元（可用 id：u1, u2）", args_schema=Ids)
    ctx2 = TaskContext(rt, CallBudget.for_task("guide"), sink)
    try:
        results, _ = await select_context(ctx2, [HumanMessage(content=(
            "你要為來源單元 u2 寫導讀，但 u2 開頭的「這兩個條件」指的是前一個單元的內容。"
            "你可以用工具 read_source_units 讀取 u1。需要讀取時 action 填 read，calls 列出要呼叫的工具與參數；"
            '不需要時 action 填 ready。只輸出 JSON，例如 {"action":"read","calls":[{"tool":"read_source_units",'
            '"args":{"unit_ids":["u1"]}}],"reason":"…"}'))], ToolDispatcher([tool]),
            task="guide", node="smoke.context", prompt_version="smoke-1")
        good = any(r.ok and "u1" in r.args.get("unit_ids", []) for r in results)
        ok &= good
        print("context   " + ("✓" if good else "✗"), [(r.tool, r.args, r.ok, r.note) for r in results])
    except TaskError as e:
        ok = False
        print("context ✗", e.kind, e.detail)
    for r in sink.records:
        print(f"  call {r.node:18s} {r.purpose:10s} {r.latency_ms} ms usage={r.usage} error={r.error_kind}")
    print(f"total {time.perf_counter() - t0:.1f} s  backend={rt.backend.name} model={rt.backend.model}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="ollama", choices=["ollama", "openvino", "mock"])
    ap.add_argument("--model", default="qwen3-vl:4b")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--model-path", default="")
    ap.add_argument("--device", default="CPU")
    sys.exit(asyncio.run(main(ap.parse_args())))
