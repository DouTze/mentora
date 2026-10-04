"""Opt-in R5 check: can the configured local model read PDF symbols from a rendered region?

    cd backend
    .venv/bin/python scripts/vision_probe.py --model qwen3-vl:4b [--pdf evals/reading/pdf/symbols.pdf] [--runs 2]

Renders each symbol-bearing paragraph of the PDF (only that region), asks the model with the same prompt and schema
the reading graph uses, and cross-checks the transcription with the font clues (bold / italic / superscript). The
model must declare vision in Ollama's /api/show; nothing is downloaded and nothing leaves the machine."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

from app.llm.ollama import OllamaBackend  # noqa: E402
from app.llm.runtime import BackendPool, CallBudget, ListSink, ModelRuntime, ModelSnapshot  # noqa: E402
from app.llm.tasks import TaskContext, TaskError, run_structured  # noqa: E402
from app.materials import extract_units  # noqa: E402
from app.reading.contracts import SymbolTranscription  # noqa: E402
from app.reading.gates import symbol_like, transcription_against_clues  # noqa: E402
from app.reading.pdf_evidence import attach_evidence, render_region, unit_symbol_evidence  # noqa: E402
from app.reading.prompts import load_bundle, render  # noqa: E402
from app.reading.source import extract_blocks  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-vl:4b")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--pdf", default=str(ROOT / "evals/reading/pdf/symbols.pdf"))
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    backend = OllamaBackend(args.host, args.model)
    caps = await backend.capabilities()
    print(json.dumps({"model": args.model, "capabilities": caps.__dict__}, ensure_ascii=False))
    if not caps.vision:
        print("the model does not declare vision: symbol units stay unverified")
        return
    units = extract_units("pdf", Path(args.pdf).read_bytes())
    _, blocks = extract_blocks("probe", "page", 1, len(units), units)
    blocks = attach_evidence(args.pdf, blocks)
    snap = ModelSnapshot("ollama", args.model, args.host, "", "", 0.2, 512, 8192)
    rt = ModelRuntime(snap, BackendPool(build=lambda c: backend))
    tmpl = load_bundle()["prompts"]["reading_symbols"]
    for b in blocks:
        ev = unit_symbol_evidence([b], args.pdf, symbol_like(b.text))
        if not ev:
            continue
        for run in range(args.runs):
            png = render_region(args.pdf, ev["regions"][0]["page"], ev["regions"][0]["bbox"])
            msg = render(tmpl, {"material_title": "probe", "position": "1/1", "quote_text": b.text,
                                "clues": "\n".join(f"- {k}：{v}" for k, v in ev["clues"].items() if v)})
            parts = [{"type": "text", "text": msg}, {"type": "image", "base64": base64.b64encode(png).decode(), "mime_type": "image/png"}]
            ctx = TaskContext(rt, CallBudget.for_task("guide"), ListSink())
            try:
                tr, recs = await run_structured(ctx, [HumanMessage(content=parts)], SymbolTranscription, task="guide",
                                                node="symbols", prompt_version="probe", meta={"step": "symbols"},
                                                min_max_tokens=1024)
            except TaskError as e:
                print(json.dumps({"block": b.text[:40], "run": run, "error": e.kind, "detail": e.detail[:200]}, ensure_ascii=False))
                continue
            problems = transcription_against_clues(tr, ev["clues"])
            print(json.dumps({"block": b.text[:40], "run": run, "clues": ev["clues"], "transcription": tr.model_dump(),
                              "problems": problems, "verdict": "confirmed" if not problems else "unverified",
                              "latency_ms": recs[-1].latency_ms, "usage": recs[-1].usage}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
