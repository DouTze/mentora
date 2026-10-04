"""Candidate guide stage for the evaluation: the guided-reading subgraph on each unit in order, previous key points
carried forward like the session does; no checkpointer, no repository."""
from __future__ import annotations

import time

from ..workflows import invoke
from ..workflows.reading_graph import ReadingDeps, ReadingUnavailable, build_reading_graph, initial_state
from .agent import ReadingTaskInput
from .contracts import LearningSnapshot
from .tools import ReadingScope


async def run_guides(r, case, quotes, parts, runtime, sink, bundle, rep, segments, ev, attach_unit_guides) -> None:
    accepted: dict[str, dict] = {}
    deps = ReadingDeps(runtime_for=lambda sid: runtime, prompts_for=lambda sid: bundle["prompts"],
                       scope_for=lambda sid, i: ReadingScope(quotes=quotes, current_index=i, guides=accepted, parts=parts),
                       sink=sink, vsrc_cache={})
    graph = build_reading_graph(deps)
    guides: list[dict | None] = []
    statuses, calls_per_unit = [], []
    unit = "page" if quotes and "頁" in quotes[0]["label"] else "line"
    for q in quotes:
        prev = [p for i in range(max(0, q["index"] - 3), q["index"]) for p in accepted.get(str(i), {}).get("key_points", [])][-6:]
        read = [{"unit_id": quotes[i]["unit_id"], "claim_id": c["id"], "statement": c["statement"]}
                for i in range(max(0, q["index"] - 3), q["index"]) for c in accepted.get(str(i), {}).get("source_claims", [])][-12:]
        task = ReadingTaskInput(session_id=case["id"], unit_id=q["unit_id"], index=q["index"], total=len(quotes),
                                label=q["label"], heading=q.get("heading"), text=q["text"], block_ids=q["block_ids"],
                                has_figure=q.get("has_figure", False), material_title=case["id"],
                                range_label=f"第 1–{len(quotes)} 段", prev_points=prev,
                                model_config_hash=runtime.snap.config_hash, prompt_bundle_hash=bundle["hash"],
                                learning=LearningSnapshot(read_claims=read),
                                source_kind="pdf" if unit == "page" else "md",
                                symbol_evidence=q.get("symbol_evidence"),
                                depends_on_prev=bool(q.get("depends_on_prev")))
        t0 = time.perf_counter()
        try:
            out = await invoke(graph, initial_state(task, op=f"{case['id']}:eval:guide:{q['unit_id']}:1"),
                               {"configurable": {"thread_id": q["unit_id"]}})
        except ReadingUnavailable as e:
            out = {"status": "failed", "error": {"code": "model_unavailable", "detail": e.detail}, "budget": {"calls": None}}
        ms = int((time.perf_counter() - t0) * 1000)
        r["guide_latency_ms"].append(ms)
        if q["index"] == 0:
            r["first_guide_ms"] = r["segment_ms"] + ms
        statuses.append({"unit": q["label"], "status": out["status"], "error": out.get("error"),
                         "calls": out.get("budget", {}).get("calls"),
                         "revised": bool(out.get("body_revisions") or out.get("source_revisions")),
                         "source_revisions": out.get("source_revisions"), "body_revisions": out.get("body_revisions"),
                         "gate1": (out.get("gate1") or {}).get("status"), "gate2": (out.get("gate2") or {}).get("status"),
                         "context_status": out.get("context_status"),
                         "bridges": len(((out.get("bridges") or {}).get("bridges")) or []),
                         "findings": [f["code"] for f in out.get("findings") or []],
                         "gate1_failing": [c["id"] + ":" + c["detail"][:80] for c in (out.get("gate1") or {}).get("checks", [])
                                           if c["status"] in ("fail", "unverified")][:6]})
        calls_per_unit.append(out.get("budget", {}).get("calls") or 0)
        if out["status"] == "accepted":
            a = out["artifact"]
            accepted[str(q["index"])] = a
            guides.append({k: a.get(k) for k in ("chinese_text", "guide", "key_points", "check")})
        else:
            guides.append(None)
            r.setdefault("guide_errors", []).append(f"{q['label']}: {out.get('error')}")
    r["guide_status"] = statuses
    r["calls_per_unit"] = calls_per_unit
    from ..llm.runtime import BUDGETS
    r["budget_exceeded"] = any(c > BUDGETS["guide"]["max_calls"] for c in calls_per_unit)
    r["retries"] = sum(1 for s in statuses if s["revised"])
    r["guides"] = guides
    r["guides_check"] = ev.check_guides(case, rep, segments, guides)
    attach_unit_guides(r, case, rep, segments, guides)
