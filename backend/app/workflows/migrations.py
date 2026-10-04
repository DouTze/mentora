"""Session upgrades between reading-workflow versions (R7).

A LangGraph session stores the full prompt bundle and versions it started with. When the reading subgraph changes
incompatibly (reading-graph-v1 → v2: source claims, Gate 1, bridges, Gate 2, verified notes), an existing session
is upgraded once, in place, before its next command runs:

- the outer graph and its checkpoints are unchanged (same nodes, same state keys), so a session waiting for the
  reader resumes where it was; a guide attempt that was cut short restarts under the new subgraph (its operation
  key carries the workflow version, so no v1 step is reused);
- the session keeps its own prompts for the tasks whose contract did not change (ask, assess, segmentation); the
  v2 source / teaching / notes / quiz prompts are added from the current bundle and the bundle gets a new hash;
- guides already published under v1 stay as they were, marked by their workflow_version (their checks were weaker;
  exports say so); they have no verified claims, so their units enter a quiz only through their raw source text;
- reading parts planned by the v1 segmentation are kept (re-planning would move the reader's progress);
- a PDF session's blocks get layout / font evidence from the stored original file (R5) when it is still there, so
  symbol-bearing units can be checked instead of being held for missing evidence; block text and ids do not change;
- the upgrade is recorded in workflow_meta["migrations"]; running it again changes nothing."""
from __future__ import annotations

from ..db import Database, now_iso
from ..llm.runtime import stable_hash
from ..reading.agent import READING_WORKFLOW_VERSION
from ..reading.contracts import CONTRACT_VERSION, TaskContract
from ..reading.prompts import BUNDLE_VERSION, load_bundle

KEEP_SESSION_PROMPTS = ("segment_blocks", "segment_seam", "ask", "assess")


def upgrade_session(db: Database, sid: str) -> dict | None:
    """Upgrade one LangGraph session to the current reading workflow. Returns the migration record, or None when
    there was nothing to do (legacy session, already current)."""
    row = db.get_session(sid)
    if not row or row.get("workflow_runner") != "langgraph":
        return None
    meta = dict(row.get("workflow_meta") or {})
    if meta.get("reading_workflow_version") == READING_WORKFLOW_VERSION:
        return None
    old = meta.get("prompt_bundle") or {"prompts": {}, "hash": None}
    current = load_bundle()
    prompts = {**current["prompts"], **{k: v for k, v in old["prompts"].items() if k in KEEP_SESSION_PROMPTS}}
    record = {"from": meta.get("reading_workflow_version"), "to": READING_WORKFLOW_VERSION, "at": now_iso(),
              "prompt_bundle_from": old.get("hash"), "kept_prompts": [k for k in KEEP_SESSION_PROMPTS if k in old["prompts"]],
              "note": "已發布的導讀保留原檢查版本；之後的段落使用來源核對與正文核對；原有的閱讀部分不重新切分"}
    meta["prompt_bundle"] = {"version": BUNDLE_VERSION, "hash": stable_hash(prompts), "prompts": prompts,
                             "upgraded_from": old.get("hash")}
    meta["reading_workflow_version"] = READING_WORKFLOW_VERSION
    meta["contract_version"] = CONTRACT_VERSION
    if "task_contract" not in meta:
        src = row["content_snapshot"].get("source") or {}
        meta["task_contract"] = TaskContract(
            scope={"material_id": src.get("material_id"), "unit": src.get("unit"), "start": src.get("start"), "end": src.get("end")},
            source_version=f"{meta.get('snapshot_id')}@{meta.get('extraction_version')}",
            defaults=["depth", "goal", "reader_background"]).model_dump()
    record["pdf_evidence"] = backfill_pdf_evidence(db, row, meta)
    meta["migrations"] = [*(meta.get("migrations") or []), record]
    import json
    with db.conn() as c:
        c.execute("UPDATE sessions SET workflow_meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), sid))
    return record


def backfill_pdf_evidence(db: Database, row: dict, meta: dict) -> str:
    """Fill `meta.pdf` of a PDF snapshot's blocks from the stored original (only blocks that have none)."""
    import json
    from pathlib import Path

    from ..materials import MaterialStore
    from ..persistence.repository import Repository
    from ..reading.pdf_evidence import attach_evidence
    src = row["content_snapshot"].get("source") or {}
    if src.get("unit") != "page" or not meta.get("snapshot_id"):
        return "not_applicable"
    repo = Repository(db)
    blocks = repo.load_blocks(meta["snapshot_id"])
    if not blocks or all(b.meta.get("pdf") for b in blocks):
        return "present"
    try:
        path, _ = MaterialStore(db).file_path(src["material_id"])
    except Exception:  # noqa: BLE001 — the material was deleted
        return "original_missing"
    if not Path(path).exists():
        return "original_missing"
    filled = attach_evidence(path, blocks)
    with db.conn() as c:
        for b in filled:
            if b.meta.get("pdf"):
                c.execute("UPDATE source_blocks SET meta=? WHERE block_id=?", (json.dumps(b.meta, ensure_ascii=False), b.block_id))
    return "backfilled"
