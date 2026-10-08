"""A 主題's guide written one 區塊 at a time (docs/reading-block-guide-plan.md).

The learning graph runs the guided-reading subgraph once per 區塊 — the same steps, checks and budget as before,
on a paragraph instead of a whole topic — and keeps what each produced. This module holds the program side: the
task of one 區塊, the 主題總摘要 prompt and its fallback, and the 主題's artifact assembled from its 區塊. The page
shows source 區塊 and guide 區塊 alternately; the top-level fields (guide, key_points, check …) keep the shape the
notes, questions, Q&A and export already read. Handouts carry no verified claims: notes and questions of a 主題 read
its source text (notes.quiz_basis)."""
from __future__ import annotations

from typing import Any

from .agent import ReadingTaskInput

TOPIC_GUIDE_VERSION = "topic-blocks-v2"


def chunks_of(q: dict) -> list[dict]:
    return q.get("chunks") or []


def outline_lines(chunks: list[dict]) -> str:
    return "\n".join(f"第 {k} 塊｜{c.get('topic') or '—'}｜{c.get('aspect') or '—'}｜{c.get('summary') or '—'}"
                     for k, c in enumerate(chunks, 1))


def fallback_overview(chunks: list[dict]) -> dict:
    """No model overview (an old prompt snapshot, an invalid answer): the 區塊 summaries joined, no key points."""
    return {"summary": "；".join(c["summary"] for c in chunks if c.get("summary"))[:200], "key_points": [], "check": "",
            "by": "program"}


# a run that broke down: the model's output could not be read (after its format fixes). Only these are run again;
# a handout that did not pass its review was already corrected in place, and redrafting it is a gamble
RETRY = {"invalid_model_output", "review_invalid"}


def worth_retrying(error: dict | None) -> bool:
    return bool(error) and (error.get("code") or "") in RETRY


def block_task(task: ReadingTaskInput, q: dict, k: int, overview: dict, evidence: dict | None) -> ReadingTaskInput:
    """The guide input of 區塊 k: its own text and source blocks (so checks and formulas are per 區塊), the 主題's
    position and reader state, and where it sits in the 主題 (its summary is what its handout must teach)."""
    chunks = chunks_of(q)
    ch = chunks[k]
    prev = chunks[k - 1] if k > 0 else None
    return task.model_copy(update={
        "unit_id": f"{task.unit_id}-b{k + 1}", "text": ch["text"], "block_ids": ch["block_ids"],
        "label": f"{task.label}・第 {k + 1}/{len(chunks)} 塊", "heading": task.heading if k == 0 else ch.get("heading"),
        "depends_on_prev": task.depends_on_prev if k == 0 else False, "symbol_evidence": evidence,
        "topic": {"unit_id": task.unit_id, "index": k, "summary": overview.get("summary") or "",
                  "chunks": [{"topic": c.get("topic", ""), "aspect": c.get("aspect", ""), "summary": c.get("summary", "")}
                             for c in chunks],
                  "prev": {"text": prev["text"], "block_ids": prev["block_ids"]} if prev else None}})


def block_entry(k: int, ch: dict, out: dict, attempt: int) -> dict:
    """What the page and the records keep of one 區塊's run."""
    base = {"index": k, "block_ids": ch["block_ids"], "source": ch["text"], "heading": ch.get("heading"),
            "summary": ch.get("summary", ""), "attempt": attempt}
    if out.get("status") == "accepted":
        a = out["artifact"]
        return {**base, "status": "accepted", "guide": a["guide"], "key_points": a.get("key_points") or [],
                "check": a.get("check") or "", "points": a.get("points") or [],
                "unverified": a.get("unverified") or [], "warnings": a.get("warnings") or [],
                "open_findings": a.get("open_findings") or [], "limits": a.get("limits") or [],
                "review_status": a.get("review_status"), "revised": a.get("revised", False), "fixes": a.get("fixes", 0),
                "fix_log": a.get("fix_log") or [], "symbols": a.get("symbols"), "calls": a.get("calls"), "error": None,
                "workflow_version": a.get("workflow_version"), "contract_version": a.get("contract_version"),
                "gate_rules": a.get("gate_rules")}
    err = out.get("error") or {"code": "quality_check_failed", "detail": ""}
    return {**base, "status": "failed", "guide": "", "error": err, "calls": (out.get("budget") or {}).get("calls")}


def assemble(task: ReadingTaskInput, q: dict, overview: dict, blocks: list[dict]) -> dict:
    """The 主題's artifact from its 區塊 (a failed 區塊 contributes no text; the page shows it as failed)."""
    ok = [b for b in blocks if b["status"] == "accepted"]
    single = len(blocks) == 1
    key_points = overview.get("key_points") or [p for b in ok for p in b.get("key_points") or []][:3]
    check = overview.get("check") or (ok[-1].get("check") if ok else "")
    warnings = [{**w, "block": b["index"]} for b in ok for w in b.get("warnings") or []]
    notes = [{**u, "block": b["index"]} for b in ok for u in b.get("unverified") or []]
    art: dict[str, Any] = {
        "mode": "blocks", "topic_version": TOPIC_GUIDE_VERSION, "unit_id": task.unit_id,
        "topic_summary": "" if single else overview.get("summary") or "", "overview_by": overview.get("by", "model"),
        "blocks": blocks, "partial": len(ok) < len(blocks),
        "guide": "\n\n".join(b["guide"] for b in ok), "key_points": key_points, "check": check,
        "limits": sorted({x for b in ok for x in b.get("limits") or []}),
        "warnings": warnings, "unverified": notes,
        "review_status": "passed_with_notes" if notes else "passed",
        "revised": any(b.get("revised") for b in ok),
        "source_refs": [i for b in blocks for i in b["block_ids"]],
        "calls": sum(b.get("calls") or 0 for b in blocks),
        "prefetched": bool(ok) and all(b.get("by") == "prefetch" for b in blocks),
    }
    first = next((b for b in ok), None)
    if first is not None:   # versions every 區塊 shares (the export names the check that produced the guide)
        for k in ("workflow_version", "contract_version", "gate_rules"):
            art[k] = first.get(k)
    return art
