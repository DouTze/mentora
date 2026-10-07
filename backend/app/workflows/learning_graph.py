"""Outer learning flow of a LangGraph session (plan §6).

```mermaid
flowchart TD
    S([start command]) --> P[prepare: plan units]
    P --> G[guide: reading subgraph]
    G --> F[finalize event]
    F -->|reading| WS{{wait_student}}
    F -->|error| WR{{wait_retry}}
    F -->|quiz| WA{{wait_answer}}
    F -->|completed| E([end])
    WS -->|ask| A[ask] --> F
    WS -->|next| N[advance] -->|part ends| C[compact]
    N -->|more units| G
    N -->|last unit| Q[generate]
    C --> G
    C --> Q
    WS -->|finish| X[finish: freeze scope] --> C
    X --> Q
    Q --> F
    WA -->|answer / retry| J[assess: model + engine.decide] --> F
    WR -->|retry| P & G & C & Q
```

Rules this graph keeps:
- The business DB is the authority. Nodes read the session row fresh, and every side effect is one
  Repository.commit() under a unique operation key; re-running a node after a restart finds the committed row and
  changes nothing (no second revision, Q&A entry, turn or quiz). The graph state only says where the flow is.
- Wait nodes call interrupt() once and do nothing else; `finalize` (pure state) records which event was applied
  before the flow waits again, so the runner can tell a delivered event from a new one.
- A model failure never escapes the graph: blocking work (segment / guide / compact / generate) stops in
  wait_retry with its error; ask / answer failures are reported on their command and the flow waits as before.
- engine.decide keeps every teaching rule; the model only judges."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .. import config
from ..db import Database
from ..llm.context import ContextItem, ContextOverflow, Packet
from ..llm.runtime import BackendPool, CallBudget, ModelRuntime, ModelSnapshot, stable_hash
from ..llm.tasks import TaskContext, TaskError, ToolDispatcher, run_structured, select_context
from ..models import ModelJudgement
from ..persistence.repository import DbSink, Repository
from ..reading import agent as ra
from ..reading import gates, notes
from ..reading import topic_guide as tg
from ..reading.contracts import LearningSnapshot, NotesAudit, NotesOutput, QuizAudit, QuizDraft, TopicOverview
from ..reading.gates import symbol_like
from ..reading.pdf_evidence import unit_symbol_evidence
from ..reading.prompts import render
from ..reading.segmentation import SEGMENTATION_VERSION, PlannerDeps, plan_parts, plan_units, to_quotes
from ..reading.tools import ReadingScope, build_tools, tools_text
from ..tutor import engine, generate, reading
from ..tutor.reading_agent import ReadingWorkspace
from . import faults, invoke
from .reading_graph import ReadingDeps, ReadingUnavailable, build_reading_graph, initial_state as guide_initial

WORKFLOW_VERSION = "learning-graph-v1"
STATE_SCHEMA_VERSION = 1
ASK_REFERS_RE = re.compile(r"前面|之前|上一段|前一段|剛才|第\s*\d+\s*段|earlier|previous")


class WorkflowState(TypedDict, total=False):
    schema_version: int
    workflow_version: str
    session_id: str
    stage: str
    pending_event: dict | None
    pending_event_id: str | None
    last_applied_event_id: str | None
    outcome: dict | None          # what the applied event led to: {status, error, result}
    error: dict | None            # blocking failure (waits for retry)
    failed_node: str | None
    cmd_error: dict | None        # non-blocking failure of the current ask / answer
    result: dict | None
    guide_attempt: dict           # unit id → attempt number (a quality failure starts a new attempt)
    guide_budget_carry: dict      # "unit:aN" → budget an interrupted run spent before the gate rules changed
    todo_compact: list
    then: str | None


@dataclass
class WorkflowDeps:
    db: Database
    repo: Repository
    pool: BackendPool
    svc: Any                      # TutorService (strategies, request builder, reading dir)
    on_units_ready: Any = None    # called with the session id once its units exist (the runner warms sources then)


def model_error(e: Exception) -> dict:
    if isinstance(e, ReadingUnavailable):
        return {"code": "model_unavailable", "detail": e.detail, "retryable": True}
    if isinstance(e, TaskError):
        code = {"unavailable": "model_unavailable", "invalid_output": "invalid_model_output", "budget": "budget_exhausted",
                "context_overflow": "context_overflow"}[e.kind]
        # a request that cannot fit the model will not fit on a retry either: the range must be made smaller
        return {"code": code, "detail": e.detail, "retryable": e.kind != "context_overflow"}
    return {"code": "internal", "detail": f"{type(e).__name__}: {e}", "retryable": True}


def build_learning_graph(deps: WorkflowDeps, checkpointer):
    repo, db = deps.repo, deps.db
    sink = DbSink(repo)

    def row(sid: str) -> dict:
        r = db.get_session(sid)
        assert r, sid
        return r

    def meta(r: dict) -> dict:
        return r["workflow_meta"]

    def runtime(sid: str) -> ModelRuntime:
        return ModelRuntime(ModelSnapshot.from_config(meta(row(sid))["model_snapshot"]), deps.pool)

    def prompts(sid: str) -> dict[str, str]:
        return meta(row(sid))["prompt_bundle"]["prompts"]

    def ctx(state: WorkflowState, task: str, op: str) -> TaskContext:
        b = CallBudget.for_task(task)
        b.calls = repo.count_calls(op)   # restart never refills the task budget
        return TaskContext(runtime(state["session_id"]), b, sink, session_id=state["session_id"],
                           command_id=state.get("pending_event_id"), operation_key=op)

    def stage(state: WorkflowState, name: str) -> None:
        repo.set_command_stage(state["session_id"], state.get("pending_event_id"), name)

    def workspace(sid: str) -> ReadingWorkspace:
        return ReadingWorkspace(deps.svc.reading_dir, sid)

    def scope(sid: str, index: int) -> ReadingScope:
        r = row(sid)
        content, rd = r["content_snapshot"], r["state"]["reading"]
        return ReadingScope(quotes=content["quotes"], current_index=index, parts=content["parts"],
                            version=meta(r).get("snapshot_id", ""),
                            guides={k: v for k, v in rd["guides"].items() if int(k) < index},
                            compacts=rd["compacts"], qa=rd["qa"])

    reading_deps = ReadingDeps(runtime_for=runtime, prompts_for=prompts, scope_for=scope, sink=sink, repo=repo,
                               on_stage=lambda sid, cid, st: repo.set_command_stage(sid, cid, st),
                               evidence_dir=Path(deps.svc.reading_dir).parent / "evidence")
    def symbol_evidence(r: dict, q: dict) -> dict | None:
        """R5: the unit's PDF symbol evidence (None for md / txt, or a unit without symbols)."""
        content = r["content_snapshot"]
        if content["source"]["unit"] != "page":
            return None
        by_id = {b.block_id: b for b in repo.load_blocks(meta(r)["snapshot_id"])}   # fresh: evidence may be backfilled
        bs = [by_id[i] for i in q["block_ids"] if i in by_id]
        try:
            path, _ = deps.svc.materials.file_path(content["source"]["material_id"])
            path = str(path) if path.exists() else None
        except Exception:  # noqa: BLE001 — a deleted material leaves no layout evidence
            path = None
        return unit_symbol_evidence(bs, path, symbol_like(q["text"]))
    reading_graph = build_reading_graph(reading_deps)

    # -- nodes ---------------------------------------------------------------------------
    async def prepare(state: WorkflowState) -> dict:
        sid = state["session_id"]
        r = row(sid)
        content = r["content_snapshot"]
        if content["segmentation"]["status"] == "done":
            return {"error": None, "failed_node": None}
        stage(state, "segment")
        m = meta(r)
        blocks = repo.load_blocks(m["snapshot_id"])
        done = {a["payload"]["key"]: a["payload"] for a in repo.list_artifacts(sid, "segment_window")}

        def make_ctx(task: str, key: str) -> TaskContext:
            return ctx(state, task, f"{sid}:segment:{key}:{state.get('pending_event_id')}")

        pd = PlannerDeps(make_ctx=make_ctx if m["planner"] == "model" else None, prompts=prompts(sid),
                         prompt_version=m["prompt_bundle"]["hash"], material_title=content["source"]["material_title"])

        def on_window(key: str, value: dict) -> None:
            repo.put_artifact(f"{sid}:segment:{key}:result", kind="segment_window", status="cached",
                              payload={"key": key, **value}, session_id=sid)
            faults.hit("after_window")

        try:
            plan = await plan_units(blocks, pd, done_windows=done, on_window=on_window)
        except TaskError as e:
            return {"error": model_error(e), "failed_node": "prepare"}
        quotes = to_quotes(content["source"]["unit"], plan, {b.block_id: b for b in blocks})
        parts, _ = plan_parts(quotes)

        def mutate(c: dict, s: dict) -> None:
            c["quotes"], c["parts"] = quotes, parts
            c["segmentation"].update({"status": "done", "planner": plan.planner, "notes": plan.warnings,
                                      "windows": len(plan.windows), "version": SEGMENTATION_VERSION,
                                      "candidates": len(blocks)})

        repo.commit(sid, f"{sid}:segment:commit", kind="segmentation", payload=plan.model_dump(), mutate=mutate)
        faults.hit("after_commit:segment")
        if deps.on_units_ready:
            deps.on_units_ready(sid)
        fresh = row(sid)["content_snapshot"]
        ws = workspace(sid)
        ws.clear_quotes()
        ws.init(fresh)
        return {"error": None, "failed_node": None}

    def learning_snapshot(r: dict, idx: int, assume_read: set[int] | None = None) -> LearningSnapshot:
        """What unit `idx`'s guide may assume about the reader: claims of units the reader moved past (read, not
        proven understood), the questions asked there, understanding evidence from answers, and the task
        contract. A guide that was generated or prefetched but not read never counts."""
        content, rd = r["content_snapshot"], r["state"]["reading"]
        tc = meta(r).get("task_contract") or {}
        read = sorted(i for i in set(rd.get("read") or []) | (assume_read or set()) if i < idx)
        claims = [{"unit_id": content["quotes"][i]["unit_id"], "claim_id": c["id"], "statement": c["statement"]}
                  for i in read[-3:] for c in (rd["guides"].get(str(i)) or {}).get("source_claims") or []][-12:]
        questions = [{"unit_id": content["quotes"][i]["unit_id"], "question": x["question"]}
                     for i in read for x in rd["qa"].get(str(i), [])][-6:]
        return LearningSnapshot(depth=tc.get("depth", "close"), goal=tc.get("goal", ""),
                                background=tc.get("reader_background", ""), read_claims=claims, open_questions=questions,
                                evidence=(rd.get("evidence") or [])[-6:])

    def guide_task(sid: str, idx: int, attempt: int = 1, guides: dict | None = None,
                   assume_read: set[int] | None = None) -> ra.ReadingTaskInput:
        """The input of unit `idx`'s guide as it stands now (`guides` overrides the accepted ones, `assume_read`
        the read set — both for prefetch)."""
        r = row(sid)
        content, rd = r["content_snapshot"], r["state"]["reading"]
        gs = rd["guides"] if guides is None else guides
        q = content["quotes"][idx]
        prev = [p for i in range(max(0, idx - 3), idx) for p in gs.get(str(i), {}).get("key_points", [])][-6:]
        hist = reading.ask_history(content["quotes"], rd, idx)
        m = meta(r)
        return ra.ReadingTaskInput(session_id=sid, unit_id=q["unit_id"], index=idx, total=len(content["quotes"]),
                                   label=q["label"], heading=q.get("heading"), text=q["text"], block_ids=q["block_ids"],
                                   has_figure=q.get("has_figure", False), material_title=content["source"]["material_title"],
                                   range_label=content["source"]["label"], prev_points=prev, qa_context=hist,
                                   model_config_hash=m["model_config_hash"], prompt_bundle_hash=m["prompt_bundle"]["hash"],
                                   attempt=attempt, learning=learning_snapshot(r, idx, assume_read),
                                   source_kind="pdf" if content["source"]["unit"] == "page" else "md",
                                   depends_on_prev=bool(q.get("depends_on_prev")),
                                   symbol_evidence=symbol_evidence(r, q))

    def prefetch_key(t: ra.ReadingTaskInput) -> str:
        """Everything the guide depends on; a question asked meanwhile or another model / prompt misses the cache."""
        return f"{t.session_id}:prefetch:{t.unit_id}:" + stable_hash([t.source_hash, t.context_hash, t.prompt_bundle_hash,
                                                                         t.model_config_hash, ra.READING_WORKFLOW_VERSION,
                                                                         gates.GATE_RULES_VERSION])

    # -- a 主題 written one 區塊 at a time (docs/reading-block-guide-plan.md) -------------------------------------
    async def topic_overview(sid: str, task: ra.ReadingTaskInput, q: dict, command_id: str | None) -> dict:
        """主題總摘要, key points and self-check from the 區塊 outlines (cached per outline); a single 區塊 needs none."""
        chunks = tg.chunks_of(q)
        if len(chunks) < 2:
            return {"summary": "", "key_points": [], "check": "", "by": "single"}
        tmpl = prompts(sid).get("reading_topic")
        if not tmpl:   # a prompt snapshot from before units-v9
            return tg.fallback_overview(chunks)
        key = f"{sid}:topic:{task.unit_id}:" + stable_hash([tg.outline_lines(chunks), tmpl, task.model_config_hash])
        done = repo.get_artifact(key)
        if done:
            return done["payload"]
        f = ra.task_fields(task)
        msg = render(tmpl, {"material_title": f["material_title"], "range_label": f["range_label"], "position": f["position"],
                            "quote_label": f["quote_label"], "count": str(len(chunks)), "chunks": tg.outline_lines(chunks)})
        b = CallBudget.for_task("topic")
        b.calls = repo.count_calls(key)
        c = TaskContext(runtime(sid), b, sink, session_id=sid, command_id=command_id, operation_key=key)
        schema = TopicOverview.model_json_schema()
        schema["properties"]["summary"] = {**schema["properties"]["summary"], "maxLength": 200}
        schema["properties"]["check"] = {**schema["properties"]["check"], "maxLength": 100}
        schema["properties"]["key_points"] = {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 3}
        try:
            ov, _ = await run_structured(c, [HumanMessage(content=msg)], TopicOverview, task="topic", node="topic",
                                         prompt_version=task.prompt_bundle_hash, meta={"step": "reading_topic"},
                                         source_ids=q["block_ids"], json_schema=schema, min_max_tokens=512,
                                         min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        except TaskError as e:
            if e.kind == "unavailable":
                raise ReadingUnavailable(e.detail) from e
            return {**tg.fallback_overview(chunks), "note": f"主題總摘要無效，改用區塊摘要：{e.detail[:120]}"}
        payload = {**ov.model_dump(), "by": "model"}
        repo.put_artifact(key, kind="topic_overview", status="cached", payload=payload, session_id=sid, unit_id=task.unit_id)
        return payload

    def block_key(t: ra.ReadingTaskInput) -> str:
        """An accepted 區塊 guide, reused by a retry of the 主題 and by the guide after a prefetch."""
        return f"{t.session_id}:block:{t.unit_id}:" + stable_hash([t.source_hash, t.context_hash, t.prompt_bundle_hash,
                                                                    t.model_config_hash, ra.READING_WORKFLOW_VERSION,
                                                                    gates.GATE_RULES_VERSION])

    async def run_blocks(sid: str, task: ra.ReadingTaskInput, q: dict, tag: str, command_id: str | None, *,
                         attempts: dict[int, int] | None = None, keep: dict[int, dict] | None = None,
                         source_only: bool = False) -> tuple[dict, list]:
        """(overview, one entry per 區塊). Each 區塊 runs the guided-reading subgraph on its own text, budget and
        operation key, concurrently (the backend pool limits real parallelism). A model that cannot be reached
        stops the whole 主題 after the others finished (their results are kept)."""
        r = row(sid)
        chunks = tg.chunks_of(q)
        keep, attempts = keep or {}, attempts or {}
        overview = tg.fallback_overview(chunks) if source_only else await topic_overview(sid, task, q, command_id)

        async def one(k: int):
            """A 區塊 that fails is run again on its own (a new attempt: new operation key, new budget) while the
            failure is one a new run can fix, up to READING_BLOCK_ATTEMPTS attempts in all."""
            if k in keep:
                return keep[k]
            first = attempts.get(k, task.attempt)
            n = max(1, config.READING_BLOCK_ATTEMPTS)
            earlier: list[dict] = []   # the attempts that failed before the last one (kept in the record)
            for i, att in enumerate(range(first, first + n)):
                entry = await run_once(k, att)
                if (source_only or entry.get("status") == "accepted" or i == n - 1
                        or not tg.worth_retrying(entry.get("error"))):
                    break
                earlier.append({"attempt": att, **(entry.get("error") or {})})
            return {**entry, "failed_attempts": earlier} if earlier and not source_only else entry

        async def run_once(k: int, att: int):
            ev = symbol_evidence(r, {**q, "block_ids": chunks[k]["block_ids"], "text": chunks[k]["text"]})
            bt = tg.block_task(task.model_copy(update={"attempt": att}), q, k, overview, ev)
            hit = None if source_only else repo.get_artifact(block_key(bt))
            if hit:
                return {**hit["payload"], "attempt": att}
            # the reader's context is in the key: a prefetch redone after a question must not reuse the cached steps
            bop = (f"{sid}:{tag}:{bt.unit_id}:a{att}:{stable_hash([bt.context_hash], 8)}:"
                   f"{ra.READING_WORKFLOW_VERSION}:{gates.GATE_RULES_VERSION}")
            # a 區塊 run redone under other gate rules keeps what the earlier run spent: the budget is never refilled
            spent = repo.count_calls(bop.rsplit(":", 1)[0] + ":") - repo.count_calls(bop + ":")
            budget = {**CallBudget.for_task("guide").to_dict(), "calls": spent} if spent > 0 else None
            out = await invoke(reading_graph, guide_initial(bt, bop, command_id, budget, source_only=source_only),
                               {"configurable": {"thread_id": bop}})
            if source_only:
                return out
            entry = {**tg.block_entry(k, chunks[k], out, att), "by": tag}
            if entry["status"] == "accepted":
                repo.put_artifact(block_key(bt), kind="guide_block", status="accepted", payload=entry, session_id=sid,
                                  unit_id=bt.unit_id)
            else:
                repo.put_artifact(f"{bop}:rejected", kind="guide", status="rejected", unit_id=bt.unit_id, session_id=sid,
                                  payload={"draft": out.get("draft"), "findings": out.get("findings"), "error": out.get("error"),
                                           "understanding": out.get("understanding"), "gate1": out.get("gate1"),
                                           "bridges": out.get("bridges"), "symbol_evidence": out.get("symbol_evidence"),
                                           "budget": out.get("budget")},
                                  source_hash=bt.source_hash, context_hash=bt.context_hash,
                                  prompt_version=bt.prompt_bundle_hash, model_config_hash=bt.model_config_hash,
                                  workflow_version=ra.READING_WORKFLOW_VERSION)
            return entry

        results = await asyncio.gather(*(one(k) for k in range(len(chunks))), return_exceptions=True)
        for x in results:
            if isinstance(x, BaseException):
                raise x
        return overview, list(results)

    async def guide_topic(state: WorkflowState, idx: int, q: dict) -> dict:
        """The guide node for a 主題 with 區塊. A retry_blocks command re-runs only the 區塊 that failed."""
        sid = state["session_id"]
        rd = row(sid)["state"]["reading"]
        existing = rd["guides"].get(str(idx))
        retry = existing is not None and (state.get("pending_event") or {}).get("type") == "retry_blocks"
        attempt = (state.get("guide_attempt") or {}).get(q["unit_id"], 1)
        keep, attempts = {}, {}
        if retry:
            keep = {b["index"]: b for b in existing.get("blocks") or [] if b["status"] == "accepted"}
            attempts = {b["index"]: b["attempt"] + 1 for b in existing.get("blocks") or [] if b["status"] != "accepted"}
        task = guide_task(sid, idx, attempt)
        op = f"{sid}:guide:{q['unit_id']}:a{attempt}:{ra.READING_WORKFLOW_VERSION}:{gates.GATE_RULES_VERSION}"
        try:
            overview, entries = await run_blocks(sid, task, q, "guide", state.get("pending_event_id"),
                                                 attempts=attempts, keep=keep)
        except ReadingUnavailable as e:
            return {"error": model_error(e), "failed_node": "guide"}
        art = {**tg.assemble(task, q, overview, entries), "backend": runtime(sid).backend.name,
               "model": runtime(sid).backend.model}
        if not any(b["status"] == "accepted" for b in entries):
            first = next(b for b in entries if b["status"] != "accepted")
            err = first.get("error") or {"code": "quality_check_failed", "detail": ""}
            detail = "；".join(f"第 {b['index'] + 1} 塊：{(b.get('error') or {}).get('detail', '')}" for b in entries)[:600]
            repo.put_artifact(f"{op}:rejected", kind="guide", status="rejected", unit_id=q["unit_id"], session_id=sid,
                              payload={"error": err, "blocks": entries, "topic_summary": art["topic_summary"]},
                              source_hash=task.source_hash, context_hash=task.context_hash,
                              prompt_version=task.prompt_bundle_hash, model_config_hash=task.model_config_hash,
                              workflow_version=ra.READING_WORKFLOW_VERSION)
            return {"error": {"code": err["code"], "detail": detail, "retryable": True}, "failed_node": "guide",
                    "guide_attempt": {**(state.get("guide_attempt") or {}), q["unit_id"]: attempt + 1}}

        def mutate(c: dict, s: dict) -> None:
            s["reading"]["guides"][str(idx)] = art
            s["reading"]["presented"] = sorted(set(s["reading"].get("presented") or []) | {idx})   # shown ≠ read

        key = f"{op}:accept" + (f":{state.get('pending_event_id')}" if retry else "")
        repo.commit(sid, key, kind="guide", payload=art, mutate=mutate, unit_id=q["unit_id"],
                    meta={"source_hash": task.source_hash, "context_hash": task.context_hash,
                          "prompt_version": task.prompt_bundle_hash, "model_config_hash": task.model_config_hash,
                          "workflow_version": ra.READING_WORKFLOW_VERSION})
        faults.hit("after_commit:guide")
        workspace(sid).write_guide(q, art)
        return {"error": None, "failed_node": None}

    async def guide(state: WorkflowState, config: RunnableConfig) -> dict:
        sid = state["session_id"]
        r = row(sid)
        content, st = r["content_snapshot"], r["state"]
        rd = st["reading"]
        retry_blocks = (state.get("pending_event") or {}).get("type") == "retry_blocks"
        if st.get("phase") != "reading" or rd["done"] or (str(rd["index"]) in rd["guides"] and not retry_blocks):
            return {"error": None, "failed_node": None}
        stage(state, "guide")
        idx = rd["index"]
        q = content["quotes"][idx]
        if tg.chunks_of(q):
            return await guide_topic(state, idx, q)
        attempt = (state.get("guide_attempt") or {}).get(q["unit_id"], 1)
        task = guide_task(sid, idx, attempt)
        op = f"{sid}:guide:{q['unit_id']}:a{attempt}:{ra.READING_WORKFLOW_VERSION}:{gates.GATE_RULES_VERSION}"
        cached = repo.get_artifact(prefetch_key(task)) if attempt == 1 else None
        try:
            if cached and cached["status"] == "accepted":
                out = {"status": "accepted", "artifact": {**cached["payload"], "prefetched": True}}
            else:
                carry = (state.get("guide_budget_carry") or {}).get(f"{q['unit_id']}:a{attempt}")
                out = await invoke(reading_graph, guide_initial(task, op, state.get("pending_event_id"), carry), config)
        except ReadingUnavailable as e:
            return {"error": model_error(e), "failed_node": "guide"}
        if out["status"] != "accepted":
            repo.put_artifact(f"{op}:rejected", kind="guide", status="rejected", unit_id=q["unit_id"], session_id=sid,
                              payload={"draft": out.get("draft"), "findings": out.get("findings"), "error": out.get("error"),
                                       "understanding": out.get("understanding"), "gate1": out.get("gate1"),
                                       "bridges": out.get("bridges"), "symbol_evidence": out.get("symbol_evidence"),
                                       "budget": out.get("budget")},
                              source_hash=task.source_hash, context_hash=task.context_hash,
                              prompt_version=task.prompt_bundle_hash, model_config_hash=task.model_config_hash,
                              workflow_version=ra.READING_WORKFLOW_VERSION)
            return {"error": {**(out.get("error") or {"code": "quality_check_failed", "detail": ""}), "retryable": True},
                    "failed_node": "guide", "guide_attempt": {**(state.get("guide_attempt") or {}), q["unit_id"]: attempt + 1}}
        art = {**out["artifact"], "backend": runtime(sid).backend.name, "model": runtime(sid).backend.model}

        def mutate(c: dict, s: dict) -> None:
            s["reading"]["guides"][str(idx)] = art
            s["reading"]["presented"] = sorted(set(s["reading"].get("presented") or []) | {idx})   # shown ≠ read

        repo.commit(sid, f"{op}:accept", kind="guide", payload=art, mutate=mutate, unit_id=q["unit_id"],
                    meta={"source_hash": task.source_hash, "context_hash": task.context_hash,
                          "prompt_version": task.prompt_bundle_hash, "model_config_hash": task.model_config_hash,
                          "workflow_version": ra.READING_WORKFLOW_VERSION})
        faults.hit("after_commit:guide")
        workspace(sid).write_guide(q, art)
        return {"error": None, "failed_node": None}

    def finalize(state: WorkflowState) -> dict:
        err = state.get("error") or state.get("cmd_error")
        outcome = {"status": "failed" if err else "completed", "error": err, "result": state.get("result")}
        return {"last_applied_event_id": state.get("pending_event_id"), "outcome": outcome}

    def wait_student(state: WorkflowState) -> dict:
        ev = interrupt({"waiting": "student"})
        return {"pending_event": ev, "pending_event_id": ev["command_id"], "cmd_error": None, "result": None}

    def wait_retry(state: WorkflowState) -> dict:
        ev = interrupt({"waiting": "retry", "failed_node": state.get("failed_node")})
        return {"pending_event": ev, "pending_event_id": ev["command_id"], "cmd_error": None, "result": None}

    def wait_answer(state: WorkflowState) -> dict:
        ev = interrupt({"waiting": "answer"})
        return {"pending_event": ev, "pending_event_id": ev["command_id"], "cmd_error": None, "result": None}

    async def ask(state: WorkflowState) -> dict:
        sid = state["session_id"]
        ev = state["pending_event"]
        question = ev["payload"]["question"]
        op = f"{sid}:{ev['command_id']}:ask"
        done = repo.get_artifact(op)
        if done:
            return {"result": {"answer": done["payload"]}}
        stage(state, "ask")
        r = row(sid)
        content, rd = r["content_snapshot"], r["state"]["reading"]
        idx = rd["index"]
        quote = content["quotes"][idx]
        history = reading.ask_history(content["quotes"], rd, idx)
        c = ctx(state, "ask", op)
        pr = prompts(sid)
        m = meta(r)
        refs: list[str] = list(quote.get("block_ids") or [])
        try:
            extra = ""
            if ASK_REFERS_RE.search(question):
                tools = build_tools(scope(sid, idx))
                sel = (f"學生在讀第 {idx + 1} 段（單元 id：{quote['unit_id']}）時問：「{question}」。若回答需要前面段落的內容，"
                       f"可用下列唯讀工具讀取；不需要就回答 ready。\n\n{tools_text(tools)}\n\n只輸出 JSON："
                       '{"action":"ready","calls":[],"reason":"…"}')
                results, _ = await select_context(c, [HumanMessage(content=sel)], ToolDispatcher(tools),
                                                  task="ask", node="ask.context", prompt_version=m["prompt_bundle"]["hash"],
                                                  meta={"quote_text": quote["text"], "question": question})
                extra = "\n\n".join(x.content for x in results if x.ok)
                refs += [u for x in results if x.ok for u in x.source_ids]
            # R2: the unit, its guide and the question are required; older Q&A and looked-up context are dropped first
            items = [ContextItem(key="quote_text", text=quote["text"], source_ids=list(quote.get("block_ids") or [])),
                     ContextItem(key="guide", text=(rd["guides"].get(str(idx)) or {}).get("guide") or "（尚未產生）"),
                     ContextItem(key="question", text=question),
                     ContextItem(key="history", text=reading.format_history(history, idx), required=False, priority=1,
                                 dropped_text="（因長度限制，較早的問答沒有提供）"),
                     ContextItem(key="ask_supplement", text=("\n\n## 補充資料（唯讀，來自本次學習已讀範圍）\n" + extra) if extra else "",
                                 required=False, priority=2, dropped_text="")]
            limits = await c.runtime.limits(ra.GUIDE_CONTEXT_TOKENS)
            try:
                body, rec = Packet("ask.answer", pr["ask"] + "{{ask_supplement}}", items,
                                   {"material_title": content["source"]["material_title"], "quote_label": quote["label"]}
                                   ).build(c.runtime.max_tokens(1024), limits)
            except ContextOverflow as e:
                raise TaskError("context_overflow", e.detail) from e
            ans, recs = await run_structured(c, [HumanMessage(content=body)], reading.Answer, task="ask", node="ask.answer",
                                             prompt_version=m["prompt_bundle"]["hash"], packet=rec,
                                             meta={"quote_text": quote["text"], "question": question},
                                             source_ids=refs, min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        except TaskError as e:
            return {"cmd_error": model_error(e)}
        last = recs[-1]
        qa = {"question": question, "answer": ans.answer, "command_id": ev["command_id"], "unit_id": quote["unit_id"],
              "source_refs": refs, "latency_ms": last.latency_ms, "usage": last.usage, "backend": last.backend,
              "model": last.model, "agent": "langgraph"}

        def mutate(cn: dict, s: dict) -> None:
            s["reading"]["qa"].setdefault(str(idx), []).append(qa)

        repo.commit(sid, op, kind="answer", payload=qa, mutate=mutate, unit_id=quote["unit_id"])
        faults.hit("after_commit:ask")
        return {"result": {"answer": qa}}

    async def advance(state: WorkflowState) -> dict:
        sid = state["session_id"]
        ev = state["pending_event"]
        r = row(sid)
        content, rd = r["content_snapshot"], r["state"]["reading"]
        op = f"{sid}:{ev['command_id']}:read"
        done = repo.get_artifact(op)
        if done:
            p = done["payload"]
        else:
            idx = rd["index"]
            quote = content["quotes"][idx]
            part = content["parts"][quote["part"]]
            needs = idx == part["quote_to"] and str(part["index"]) not in rd["compacts"] and len(content["parts"]) > 1
            finished = idx + 1 >= len(content["quotes"])
            p = {"index": idx, "compact": [part["index"]] if needs else [], "then": "generate" if finished else "guide"}

            def mutate(c: dict, s: dict) -> None:
                x = s["reading"]
                x["read"] = sorted(set(x.get("read", [])) | {idx})
                if finished:
                    x["done"] = True
                else:
                    x["index"] = idx + 1

            repo.commit(sid, op, kind="read", payload=p, mutate=mutate, unit_id=quote["unit_id"])
            faults.hit("after_commit:next")
        return {"todo_compact": p["compact"], "then": p["then"], "result": {"read": p["index"]}}

    async def finish(state: WorkflowState) -> dict:
        sid = state["session_id"]
        ev = state["pending_event"]
        r = row(sid)
        content, rd = r["content_snapshot"], r["state"]["reading"]
        op = f"{sid}:{ev['command_id']}:finish"
        done = repo.get_artifact(op)
        if done:
            p = done["payload"]
        else:
            idx = rd["index"]
            upto = idx if str(idx) in rd["guides"] else idx - 1   # a unit whose guide failed was never shown
            last_part = content["quotes"][upto]["part"]
            todo = [pt["index"] for pt in content["parts"][:last_part + 1] if str(pt["index"]) not in rd["compacts"]] \
                if len(content["parts"]) > 1 else []
            p = {"upto": upto, "compact": todo}

            def mutate(c: dict, s: dict) -> None:
                x = s["reading"]
                x["read"] = sorted(set(x.get("read", [])) | {upto})
                x.update({"done": True, "stopped_at": upto, "index": upto})

            repo.commit(sid, op, kind="finish", payload=p, mutate=mutate)
            faults.hit("after_commit:finish")
        return {"todo_compact": p["compact"], "then": "generate", "result": {"stopped_at": p["upto"]},
                "error": None, "failed_node": None}

    async def compact(state: WorkflowState) -> dict:
        """R6: notes of a finished part, compressed from its verified claims (never from raw text), each note bound to
        the claims it compresses and checked (markers, formulas, audit). Verified claims no verified note covers stay
        available as they are. If the notes cannot be produced (invalid output, budget), the part keeps its verified
        claims as the review / quiz basis and records why; only an unreachable model blocks (retry)."""
        sid = state["session_id"]
        for pi in list(state.get("todo_compact") or []):
            r = row(sid)
            content, rd = r["content_snapshot"], r["state"]["reading"]
            if str(pi) in rd["compacts"]:
                continue
            stage(state, "compact")
            part = dict(content["parts"][pi])
            upto = rd.get("stopped_at")
            if upto is not None and upto < part["quote_to"]:
                part["quote_to"] = upto
                part["label"] += f"（讀到第 {upto + 1} 段）"
            op = f"{sid}:compact:{pi}:{part['quote_to']}"          # one commit per part …
            call_op = f"{op}:{state.get('pending_event_id')}"     # … a fresh call budget per command (a retry)
            cmap = notes.part_claims(content["quotes"], rd["guides"], part["quote_from"], part["quote_to"])
            try:
                item = await compact_part(state, r, part, cmap, call_op)
            except TaskError as e:
                if e.kind == "unavailable":
                    return {"error": model_error(e), "failed_node": "compact"}
                item = {"notes": [], "gaps": list(cmap), "status": "notes_unavailable",
                        "reason": f"{model_error(e)['code']}: {e.detail[:160]}"}
            item.update({"unit_ids": [q["unit_id"] for q in content["quotes"][part["quote_from"]:part["quote_to"] + 1]],
                         "version": notes.NOTES_VERSION, "agent": "langgraph"})
            ok = [n for n in item["notes"] if n["status"] == "verified"]
            vn = [notes.VerifiedNote.model_validate(n) for n in item["notes"]]
            item["markdown"] = notes.notes_markdown(part, vn, item["gaps"], cmap) if cmap else \
                f"### {part['label']}\n\n（這一部分沒有通過核對的內容，無法整理筆記。）\n"
            # legacy fields (read_compact tool, quiz_source_text, exports)
            item["summary"] = " ".join(n["text"] for n in ok)
            item["claims"] = [n["text"] for n in ok] + [cmap[k]["claim"]["statement"] for k in item["gaps"]]
            item["key_terms"] = [n["term"] for n in ok if n["kind"] == "term" and n["term"]]
            item["claim_sources"] = {n["id"]: n["claim_refs"] for n in item["notes"]}

            def mutate(c: dict, s: dict, pi=pi, item=item) -> None:
                s["reading"]["compacts"][str(pi)] = item

            repo.commit(sid, op, kind="compact", payload=item, mutate=mutate)
            workspace(sid).write_compact(part, item["markdown"])
        return {"todo_compact": [], "error": None, "failed_node": None}

    async def compact_part(state: WorkflowState, r: dict, part: dict, cmap: dict, call_op: str) -> dict:
        if not cmap:
            return {"notes": [], "gaps": [], "status": "no_basis", "reason": "這一部分沒有通過來源核對的段落"}
        sid = state["session_id"]
        c = ctx(state, "compact", call_op)
        pv = meta(r)["prompt_bundle"]["hash"]
        pr = prompts(sid)
        fields = {"material_title": r["content_snapshot"]["source"]["material_title"], "part_label": part["label"],
                  "claims": notes.claims_listing(cmap)}
        claims_meta = {k: x["claim"]["statement"] for k, x in cmap.items()}
        src_ids = sorted({x["unit_id"] for x in cmap.values()})

        async def write(template: str, extra: dict, step: str):
            out, _ = await run_structured(c, [HumanMessage(content=render(pr[template], {**fields, **extra}))], NotesOutput,
                                          task="compact", node=step, prompt_version=pv, source_ids=src_ids,
                                          check=notes.notes_check(cmap), json_schema=notes.notes_schema(cmap),
                                          prose_fields=("text",),
                                          meta={"step": step, "claims_map": claims_meta},
                                          min_max_tokens=3072, min_context_tokens=16384)
            return out

        async def audit(out: NotesOutput) -> NotesAudit:
            ids = [n.id for n in out.notes]
            tmpl = json.dumps({"notes": [{"id": i, "verdict": "faithful", "note": ""} for i in ids]}, ensure_ascii=False)
            listing = "\n".join(f"- {n.id}［{n.kind}］{n.text}（依據 {'、'.join(n.claim_refs)}）" for n in out.notes)
            a, _ = await run_structured(c, [HumanMessage(content=render(pr["reading_notes_audit"], {
                **fields, "notes": listing, "output_template": tmpl}))], NotesAudit, task="compact", node="notes_audit",
                prompt_version=pv, source_ids=src_ids, json_schema=gates.exact_ids_schema(NotesAudit, {"notes": ids}),
                meta={"step": "notes_audit", "note_ids": ids}, min_max_tokens=2048, min_context_tokens=16384)
            return a

        out = await write("reading_notes", {}, "notes")
        vn = notes.verify_notes(out, cmap, await audit(out))
        gaps = notes.coverage_gaps(vn, cmap)
        revised = False
        if (gaps or any(n.status != "verified" for n in vn)) and c.budget.calls + 2 <= c.budget.max_calls:
            out = await write("reading_notes_revise", {"issues": notes.notes_issues(vn, gaps, cmap),
                                                         "notes_json": out.model_dump_json()}, "notes_revise")
            vn = notes.verify_notes(out, cmap, await audit(out))
            gaps = notes.coverage_gaps(vn, cmap)
            revised = True
        return {"notes": [n.model_dump() for n in vn], "gaps": gaps, "revised": revised,
                "status": "verified" if not gaps and all(n.status == "verified" for n in vn) else "partial"}

    async def generate_quiz(state: WorkflowState) -> dict:
        """R6: questions only from verified basis of the read range (verified notes, else verified claims), on a
        program coverage plan; every concept and question cites its basis and passes an audit. No basis → no quiz."""
        sid = state["session_id"]
        r = row(sid)
        content, st = r["content_snapshot"], r["state"]
        if st.get("phase") != "reading":
            return {"error": None, "failed_node": None}
        stage(state, "generate")
        rd = st["reading"]
        upto = rd.get("stopped_at")
        read = content["quotes"] if upto is None else content["quotes"][:upto + 1]
        last = read[-1]["index"]
        basis = notes.quiz_basis(content, rd, last)
        if not basis:
            return {"error": {"code": "quiz_basis_insufficient", "retryable": False,
                              "detail": "已讀範圍沒有通過來源核對的內容，無法依據來源出題。"}, "failed_node": "generate"}
        plan = notes.coverage_plan(content, rd, basis, last, generate.MAX_CONCEPTS)
        src = content["source"]
        label = generate.range_label(src["unit"], src["start"], src["end"])
        pv = meta(r)["prompt_bundle"]["hash"]
        pr = prompts(sid)
        op = f"{sid}:generate:{upto}"
        c = ctx(state, "generate", f"{op}:{state.get('pending_event_id')}")
        ids = {b["id"] for b in basis}
        fields = {"material_title": src["material_title"], "range_label": label, "basis": notes.basis_listing(basis),
                  "plan": notes.plan_listing(plan)}
        try:
            draft, _ = await run_structured(c, [HumanMessage(content=render(pr["quiz_generate"], fields))], QuizDraft,
                                            task="generate", node="quiz_generate", prompt_version=pv,
                                            check=notes.quiz_check(plan, ids), source_ids=[q["unit_id"] for q in read],
                                            json_schema=notes.quiz_schema(plan, sorted(ids)),
                                            meta={"step": "quiz_generate", "plan": plan}, min_max_tokens=4096,
                                            min_context_tokens=16384)
            qids = notes.quiz_audit_ids(draft)
            questions = "\n".join(f"- {cc.topic}.{k}：{getattr(cc, k).prompt}（依據 {'、'.join(getattr(cc, k).basis)}；"
                                  f"評分：{getattr(cc, k).rubric}）" for cc in draft.concepts for k in ("diagnostic", "transfer"))
            tmpl = json.dumps({"questions": [{"id": i, "verdict": "ok", "note": ""} for i in qids]}, ensure_ascii=False)
            audit, _ = await run_structured(c, [HumanMessage(content=render(pr["quiz_audit"], {
                **fields, "questions": questions, "output_template": tmpl}))], QuizAudit, task="generate",
                node="quiz_audit", prompt_version=pv, json_schema=gates.exact_ids_schema(QuizAudit, {"questions": qids}),
                meta={"step": "quiz_audit", "question_ids": qids}, min_max_tokens=2048, min_context_tokens=16384)
        except TaskError as e:
            return {"error": model_error(e), "failed_node": "generate"}
        kept, dropped = notes.quiz_keep(draft, audit)
        if not kept.concepts:
            return {"error": {"code": "quiz_unverified", "retryable": True,
                              "detail": "題目沒有通過依據核對：" + "；".join(dropped)[:300]}, "failed_node": "generate"}
        gen = generate.Generated(title=kept.title, concepts=[generate.GenConcept(
            title=x.title, explanation=x.explanation, examples=x.examples, key_terms=x.key_terms,
            diagnostic=generate.GenQuestion(**x.diagnostic.model_dump(exclude={"basis"})),
            transfer=generate.GenQuestion(**x.transfer.model_dump(exclude={"basis"}))) for x in kept.concepts])
        by_id = {b["id"]: b for b in basis}
        review = notes.symbol_consistency([(rd["compacts"].get(str(p["index"])) or {}).get("notes", []) for p in content["parts"]])

        def mutate(cn: dict, s: dict) -> None:
            generate.fill_content(cn, gen)
            for k, x in enumerate(kept.concepts):   # each concept / question keeps the basis it was checked against
                cn["concepts"][k]["basis"] = x.basis
                cn["concepts"][k]["source_refs"] = sorted({u for b in x.basis for u in by_id[b]["unit_ids"]})
                for q in cn["questions"]:
                    if q["concept_id"] == cn["concepts"][k]["id"]:
                        q["basis"] = getattr(x, q["type"]).basis
            new = engine.initial_state(cn)
            new["reading"] = s["reading"]
            new["reading"]["quiz_source"] = "verified"
            new["reading"]["quiz_scope"] = [q["unit_id"] for q in read]
            new["reading"]["quiz_plan"] = {"topics": [{k: t[k] for k in ("id", "label", "basis")} for t in plan],
                                           "dropped": dropped, "notation_review": review}
            s.clear()
            s.update(new)

        repo.commit(sid, op + ":commit", kind="quiz", payload={"draft": kept.model_dump(), "dropped": dropped}, mutate=mutate)
        faults.hit("after_commit:generate")
        return {"error": None, "failed_node": None}

    async def assess(state: WorkflowState) -> dict:
        sid = state["session_id"]
        ev = state["pending_event"]
        answer = ev["payload"]["answer"]
        op = f"{sid}:{ev['command_id']}:assess"
        done = repo.get_artifact(op)
        if done:
            return {"result": done["payload"]}
        stage(state, "assess")
        r = row(sid)
        content, strategy, st = r["content_snapshot"], r["strategy_snapshot"], r["state"]
        q = engine.get_question(content, st["question_id"])
        concept = engine.get_concept(content, q["concept_id"])
        messages, mmeta = deps.svc._build_request(content, strategy, st, q, concept, answer, sid)
        rt = runtime(sid)
        base_turn: dict[str, Any] = {
            "concept_id": q["concept_id"], "question_id": q["id"], "phase": st["phase"], "student_answer": answer,
            "model_request": {"messages": messages, "meta": mmeta}, "state_before": st, "backend": rt.backend.name,
            "model": rt.backend.model, "command_id": ev["command_id"]}
        try:
            j, recs = await run_structured(ctx(state, "assess", op), [HumanMessage(content=messages[0]["content"])],
                                           ModelJudgement, task="assess", node="assess",
                                           prompt_version=meta(r)["prompt_bundle"]["hash"], meta=mmeta,
                                           source_ids=[concept["id"]])
        except TaskError as e:
            err = model_error(e)
            last = (e.records or [None])[-1]
            turn = {**base_turn, "model_raw": getattr(last, "raw", None), "latency_ms": getattr(last, "latency_ms", None),
                    "usage": getattr(last, "usage", None),
                    "error": f"{'connection' if err['code'] == 'model_unavailable' else 'invalid_output'}: {err['detail']}"}
            repo.commit(sid, op + ":error", kind="assess_error", payload=err, turn=turn, artifact_status="rejected")
            return {"cmd_error": err}
        valid = {c["id"] for c in content["concepts"]}
        notes = []
        unknown = [x for x in j.source_ids if x not in valid]
        if unknown:
            notes.append(f"移除不存在的教材引用：{unknown}")
            j.source_ids = [x for x in j.source_ids if x in valid]
        if len(recs) > 1:
            notes.insert(0, f"第一次輸出無效（{recs[0].error}），重試一次後成功")
        new_state, msg, decision = engine.decide(st, j, strategy, content)
        last = recs[-1]
        turn = {**base_turn, "model_raw": last.raw, "judgement": j.model_dump(), "validation_notes": notes,
                "decision": decision, "tutor_message": msg.model_dump(), "state_after": new_state,
                "latency_ms": last.latency_ms, "usage": last.usage}
        status, summary = "active", None
        if new_state["phase"] == "completed":
            status = "completed"
            summary = engine.build_summary(new_state, content, db.list_turns(sid) + [turn])
        result = {"message": msg.model_dump(), "decision": decision}

        def mutate(c: dict, s: dict) -> None:
            s.clear()
            s.update(new_state)

        repo.commit(sid, op, kind="assess", payload=result, mutate=mutate, status=status, summary=summary, turn=turn)
        faults.hit("after_commit:assess")
        return {"result": result}

    # -- routing ---------------------------------------------------------------------------
    def after_finalize(state: WorkflowState) -> str:
        r = row(state["session_id"])
        if r["status"] == "completed":
            return END
        if state.get("error"):
            return "wait_retry"
        return "wait_student" if r["state"].get("phase") == "reading" else "wait_answer"

    def from_student(state: WorkflowState) -> str:
        ev = state["pending_event"]
        t = ev["type"]
        if t == "retry":
            return "ask"
        return {"ask": "ask", "next": "advance", "finish": "finish", "guide": "finalize", "retry_blocks": "guide"}[t]

    def from_retry(state: WorkflowState) -> str:
        if state["pending_event"]["type"] == "finish":
            return "finish"
        return {"prepare": "prepare", "guide": "guide", "compact": "compact", "generate": "generate"}[state["failed_node"]]

    def blocked_or(nxt: str):
        return lambda s: "finalize" if s.get("error") else nxt

    def after_compact(state: WorkflowState) -> str:
        return "finalize" if state.get("error") else (state.get("then") or "guide")

    def after_advance(state: WorkflowState) -> str:
        return "compact" if state.get("todo_compact") else state["then"]

    g = StateGraph(WorkflowState)
    for name, fn in (("prepare", prepare), ("guide", guide), ("finalize", finalize), ("wait_student", wait_student),
                     ("wait_retry", wait_retry), ("wait_answer", wait_answer), ("ask", ask), ("advance", advance),
                     ("finish", finish), ("compact", compact), ("generate", generate_quiz), ("assess", assess)):
        g.add_node(name, fn)
    g.add_edge(START, "prepare")
    g.add_conditional_edges("prepare", blocked_or("guide"), ["guide", "finalize"])
    g.add_edge("guide", "finalize")
    g.add_conditional_edges("finalize", after_finalize, ["wait_student", "wait_retry", "wait_answer", END])
    g.add_conditional_edges("wait_student", from_student, ["ask", "advance", "finish", "finalize", "guide"])
    g.add_conditional_edges("wait_retry", from_retry, ["prepare", "guide", "compact", "generate", "finish"])
    g.add_edge("wait_answer", "assess")
    g.add_edge("ask", "finalize")
    g.add_conditional_edges("advance", after_advance, ["compact", "guide", "generate"])
    g.add_conditional_edges("finish", after_advance, ["compact", "generate"])
    g.add_conditional_edges("compact", after_compact, ["guide", "generate", "finalize"])
    g.add_edge("generate", "finalize")
    g.add_edge("assess", "finalize")
    compiled = g.compile(checkpointer=checkpointer)

    async def prefetch_next(sid: str) -> str:
        """Write the next unit's guide ahead of time, read-only for the session: the result is a cache artifact
        the guide node may adopt; reading progress, Q&A and the main graph are never touched. Returns what it did."""
        r = row(sid)
        content, st = r["content_snapshot"], r["state"]
        rd = st.get("reading") or {}
        if st.get("phase") != "reading" or rd.get("done") or str(rd.get("index")) not in rd.get("guides", {}):
            return "not_applicable"
        nxt = rd["index"] + 1
        if nxt >= len(content["quotes"]) or str(nxt) in rd["guides"]:
            return "not_applicable"
        # the next unit's input once the current unit is read: the current guide counts as accepted
        task = guide_task(sid, nxt, 1, guides=rd["guides"], assume_read={rd["index"]})
        q = content["quotes"][nxt]
        if tg.chunks_of(q):   # 區塊 are cached one by one; the guide node finds the accepted ones
            _, entries = await run_blocks(sid, task, q, "prefetch", "prefetch")
            return "written" if all(b["status"] == "accepted" for b in entries) else                 "partial" if any(b["status"] == "accepted" for b in entries) else "failed"
        key = prefetch_key(task)
        if repo.get_artifact(key):
            return "cached"
        out = await invoke(reading_graph, guide_initial(task, key + ":run", "prefetch"), {"configurable": {"thread_id": key}})
        if out["status"] == "accepted":
            repo.put_artifact(key, kind="guide_prefetch", status="accepted", unit_id=task.unit_id, session_id=sid,
                              payload={**out["artifact"], "backend": runtime(sid).backend.name, "model": runtime(sid).backend.model},
                              source_hash=task.source_hash, context_hash=task.context_hash,
                              prompt_version=task.prompt_bundle_hash, model_config_hash=task.model_config_hash,
                              workflow_version=ra.READING_WORKFLOW_VERSION)
            return "written"
        return "failed"

    async def warm_sources(sid: str, slots: int) -> list[str]:
        """Verify the sources of the units after the next one, concurrently (up to `slots` at a time): Gate 1 does not
        depend on the reader, and its VerifiedSource is cached, so when the reader gets there only the reader-
        dependent part of the guide (bridges, body, Gate 2) is left. Read-only for the session; the unit right after
        the current one is the prefetch's. Returns what happened per unit."""
        r = row(sid)
        content, st = r["content_snapshot"], r["state"]
        rd = st.get("reading") or {}
        if st.get("phase") != "reading" or rd.get("done") or slots < 1 or (content.get("segmentation") or {}).get("status") != "done":
            return []
        first = rd["index"] + 2
        todo = [i for i in range(first, min(len(content["quotes"]), first + slots)) if str(i) not in rd["guides"]]

        async def one(i: int) -> str:
            task = guide_task(sid, i, 1, guides=rd["guides"])
            q = content["quotes"][i]
            if tg.chunks_of(q):   # the verified source is per 區塊
                _, outs = await run_blocks(sid, task, q, "warm", "warm", source_only=True)
                codes = {{"source_verified": "verified"}.get(o["status"], (o.get("error") or {}).get("code") or o["status"])
                         for o in outs}
                return codes.pop() if len(codes) == 1 else "mixed:" + ",".join(sorted(codes))
            key = f"{sid}:warm:{task.unit_id}:" + stable_hash([task.source_hash, task.prompt_bundle_hash,
                                                              task.model_config_hash, gates.GATE_RULES_VERSION])
            out = await invoke(reading_graph, guide_initial(task, key, "warm", source_only=True), {"configurable": {"thread_id": key}})
            return {"source_verified": "verified"}.get(out["status"], (out.get("error") or {}).get("code") or out["status"])
        return list(await asyncio.gather(*(one(i) for i in todo), return_exceptions=False))

    compiled.prefetch_next = prefetch_next   # used by the runner between commands
    compiled.warm_sources = warm_sources
    return compiled


def initial_input(session_id: str, start_command_id: str) -> WorkflowState:
    return {"schema_version": STATE_SCHEMA_VERSION, "workflow_version": WORKFLOW_VERSION, "session_id": session_id,
            "stage": "segment", "pending_event": {"type": "start", "command_id": start_command_id, "payload": {}},
            "pending_event_id": start_command_id, "last_applied_event_id": None, "outcome": None, "error": None,
            "failed_node": None, "cmd_error": None, "result": None, "guide_attempt": {}, "todo_compact": [], "then": None}


def thread_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": f"session:{session_id}"}}
