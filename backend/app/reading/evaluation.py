"""Candidate pipeline for scripts/eval_reading.py: the same code paths the LangGraph workflow uses, run on one
fixture without a session (no DB, no graph), so segmentation and guide quality can be compared with the legacy run."""
from __future__ import annotations

import time
from pathlib import Path

from ..llm.runtime import BackendPool, CallBudget, ListSink, ModelRuntime, ModelSnapshot
from ..llm.tasks import TaskContext
from .prompts import load_bundle
from .segmentation import PlannerDeps, plan_parts, plan_units, to_quotes
from .source import extract_blocks


async def run_case(case: dict, cfg, counter, stages: list[str], tmp: Path, *, ev, attach_unit_guides) -> dict:
    unit, units = ev.load_units(case)
    snap = ModelSnapshot.from_config(cfg)
    pool = BackendPool(build=lambda _cfg: counter)
    runtime = ModelRuntime(snap, pool)
    sink = ListSink()
    bundle = load_bundle()
    r: dict = {"id": case["id"], "fallback_notes": [], "calls": [], "guide_latency_ms": []}

    def make_ctx(task: str, key: str) -> TaskContext:
        return TaskContext(runtime, CallBudget.for_task(task), sink, operation_key=f"{case['id']}:{task}:{key}")

    t0 = time.perf_counter()
    _, blocks = extract_blocks(case["id"], unit, 1, len(units), units)
    deps = PlannerDeps(make_ctx=make_ctx if "segment" in stages else None, prompts=bundle["prompts"],
                       prompt_version=bundle["hash"], material_title=case["id"])
    plan = await plan_units(blocks, deps, window_chars=case.get("window_chars") or 5000)
    by_id = {b.block_id: b for b in blocks}
    quotes = to_quotes(unit, plan, by_id)
    parts, _ = plan_parts(quotes)
    r["segment_ms"] = int((time.perf_counter() - t0) * 1000)
    r["fallback_notes"] = plan.warnings
    r["planner"] = plan.planner
    segments = [{"text": q["text"], "heading": q.get("heading")} for q in quotes]
    rep = ev.check_segmentation(case, unit, units, segments)
    r["unit_count"] = len(quotes)
    r["segmentation"] = rep.as_dict()
    r["units"] = [{"label": q["label"], "heading": q.get("heading"), "text": q["text"], "reason": q["boundary_reason"],
                   "block_ids": q["block_ids"]} for q in quotes]
    if "guide" in stages:
        from .evaluation_guides import run_guides   # P3
        await run_guides(r, case, quotes, parts, runtime, sink, bundle, rep, segments, ev, attach_unit_guides)
    if case.get("flow"):
        upto = case["flow"]["read_units"] - 1
        r["quiz_scope"] = ev.check_quiz_scope(case, "\n\n".join(q["text"] for q in quotes[:upto + 1]))
    r["calls"] += counter.take()
    r["llm_trace"] = [{"node": x.node, "purpose": x.purpose, "error_kind": x.error_kind, "latency_ms": x.latency_ms}
                      for x in sink.records]
    return r
