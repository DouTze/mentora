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
from ..llm.runtime import BackendPool, CallBudget, ModelRuntime, ModelSnapshot, stable_hash
from ..llm.tasks import TaskContext, TaskError, ToolDispatcher, run_structured, select_context
from ..models import ModelJudgement
from ..persistence.repository import DbSink, Repository
from ..reading import agent as ra
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
    todo_compact: list
    then: str | None


@dataclass
class WorkflowDeps:
    db: Database
    repo: Repository
    pool: BackendPool
    svc: Any                      # TutorService (strategies, request builder, reading dir)


def model_error(e: Exception) -> dict:
    if isinstance(e, ReadingUnavailable):
        return {"code": "model_unavailable", "detail": e.detail, "retryable": True}
    if isinstance(e, TaskError):
        code = {"unavailable": "model_unavailable", "invalid_output": "invalid_model_output", "budget": "budget_exhausted"}[e.kind]
        return {"code": code, "detail": e.detail, "retryable": True}
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
                            guides={k: v for k, v in rd["guides"].items() if int(k) < index},
                            compacts=rd["compacts"], qa=rd["qa"])

    reading_deps = ReadingDeps(runtime_for=runtime, prompts_for=prompts, scope_for=scope, sink=sink, repo=repo,
                               on_stage=lambda sid, cid, st: repo.set_command_stage(sid, cid, st))
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
        fresh = row(sid)["content_snapshot"]
        ws = workspace(sid)
        ws.clear_quotes()
        ws.init(fresh)
        return {"error": None, "failed_node": None}

    def guide_task(sid: str, idx: int, attempt: int = 1, guides: dict | None = None) -> ra.ReadingTaskInput:
        """The input of unit `idx`'s guide as it stands now (`guides` overrides the accepted ones, for prefetch)."""
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
                                   attempt=attempt)

    def prefetch_key(t: ra.ReadingTaskInput) -> str:
        """Everything the guide depends on; a question asked meanwhile or another model / prompt misses the cache."""
        return f"{t.session_id}:prefetch:{t.unit_id}:" + stable_hash([t.source_hash, t.context_hash, t.prompt_bundle_hash,
                                                                         t.model_config_hash, ra.READING_WORKFLOW_VERSION])

    async def guide(state: WorkflowState, config: RunnableConfig) -> dict:
        sid = state["session_id"]
        r = row(sid)
        content, st = r["content_snapshot"], r["state"]
        rd = st["reading"]
        if st.get("phase") != "reading" or rd["done"] or str(rd["index"]) in rd["guides"]:
            return {"error": None, "failed_node": None}
        stage(state, "guide")
        idx = rd["index"]
        q = content["quotes"][idx]
        attempt = (state.get("guide_attempt") or {}).get(q["unit_id"], 1)
        task = guide_task(sid, idx, attempt)
        op = f"{sid}:guide:{q['unit_id']}:a{attempt}:{ra.READING_WORKFLOW_VERSION}"
        cached = repo.get_artifact(prefetch_key(task)) if attempt == 1 else None
        try:
            if cached and cached["status"] == "accepted":
                out = {"status": "accepted", "artifact": {**cached["payload"], "prefetched": True}}
            else:
                out = await invoke(reading_graph, guide_initial(task, op, state.get("pending_event_id")), config)
        except ReadingUnavailable as e:
            return {"error": model_error(e), "failed_node": "guide"}
        if out["status"] != "accepted":
            repo.put_artifact(f"{op}:rejected", kind="guide", status="rejected", unit_id=q["unit_id"], session_id=sid,
                              payload={"draft": out.get("draft"), "findings": out.get("findings"), "error": out.get("error")},
                              source_hash=task.source_hash, context_hash=task.context_hash,
                              prompt_version=task.prompt_bundle_hash, model_config_hash=task.model_config_hash,
                              workflow_version=ra.READING_WORKFLOW_VERSION)
            return {"error": {**(out.get("error") or {"code": "quality_check_failed", "detail": ""}), "retryable": True},
                    "failed_node": "guide", "guide_attempt": {**(state.get("guide_attempt") or {}), q["unit_id"]: attempt + 1}}
        art = {**out["artifact"], "backend": runtime(sid).backend.name, "model": runtime(sid).backend.model}

        def mutate(c: dict, s: dict) -> None:
            s["reading"]["guides"][str(idx)] = art

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
        body = render(pr["ask"], {"material_title": content["source"]["material_title"], "quote_label": quote["label"],
                                  "quote_text": quote["text"], "guide": (rd["guides"].get(str(idx)) or {}).get("guide") or "（尚未產生）",
                                  "history": reading.format_history(history, idx), "question": question})
        refs: list[str] = list(quote.get("block_ids") or [])
        try:
            if ASK_REFERS_RE.search(question):
                tools = build_tools(scope(sid, idx))
                sel = (f"學生在讀第 {idx + 1} 段（單元 id：{quote['unit_id']}）時問：「{question}」。若回答需要前面段落的內容，"
                       f"可用下列唯讀工具讀取；不需要就回答 ready。\n\n{tools_text(tools)}\n\n只輸出 JSON："
                       '{"action":"ready","calls":[],"reason":"…"}')
                results, _ = await select_context(c, [HumanMessage(content=sel)], ToolDispatcher(tools, max_chars=2500),
                                                  task="ask", node="ask.context", prompt_version=m["prompt_bundle"]["hash"],
                                                  meta={"quote_text": quote["text"], "question": question})
                extra = "\n\n".join(x.content for x in results if x.ok)
                if extra:
                    body += "\n\n## 補充資料（唯讀，來自本次學習已讀範圍）\n" + extra
                    refs += [u for x in results if x.ok for u in (x.args.get("unit_ids") or [x.args.get("unit_id")]) if u]
            ans, recs = await run_structured(c, [HumanMessage(content=body)], reading.Answer, task="ask", node="ask.answer",
                                             prompt_version=m["prompt_bundle"]["hash"],
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
            qs = content["quotes"][part["quote_from"]:part["quote_to"] + 1]
            op = f"{sid}:compact:{pi}:{part['quote_to']}"          # one commit per part …
            call_op = f"{op}:{state.get('pending_event_id')}"     # … a fresh call budget per command (a retry)
            body = render(prompts(sid)["compact"], {"material_title": content["source"]["material_title"],
                                                    "part_label": part["label"],
                                                    "part_text": "\n\n".join(f"[{q['label']}]\n{q['text']}" for q in qs)})
            try:
                cp, recs = await run_structured(ctx(state, "compact", call_op), [HumanMessage(content=body)], reading.Compact,
                                                task="compact", node="compact", prompt_version=meta(r)["prompt_bundle"]["hash"],
                                                meta={"part_text": "\n\n".join(q["text"] for q in qs)},
                                                source_ids=[b for q in qs for b in q.get("block_ids", [])],
                                                min_max_tokens=2048, min_context_tokens=16384)
            except TaskError as e:
                return {"error": model_error(e), "failed_node": "compact"}
            md = reading.compact_markdown(part, cp)
            item = {**cp.model_dump(), "markdown": md, "unit_ids": [q["unit_id"] for q in qs],
                    "claim_sources": [q["unit_id"] for q in qs], "latency_ms": recs[-1].latency_ms,
                    "backend": recs[-1].backend, "model": recs[-1].model, "agent": "langgraph"}

            def mutate(c: dict, s: dict, pi=pi, item=item) -> None:
                s["reading"]["compacts"][str(pi)] = item

            repo.commit(sid, op, kind="compact", payload=item, mutate=mutate)
            workspace(sid).write_compact(part, md)
        return {"todo_compact": [], "error": None, "failed_node": None}

    async def generate_quiz(state: WorkflowState) -> dict:
        sid = state["session_id"]
        r = row(sid)
        content, st = r["content_snapshot"], r["state"]
        if st.get("phase") != "reading":
            return {"error": None, "failed_node": None}
        stage(state, "generate")
        rd = st["reading"]
        upto = rd.get("stopped_at")
        read = content["quotes"] if upto is None else content["quotes"][:upto + 1]
        read_text = "\n\n".join(q["text"] for q in read)
        read_parts = [p for p in content["parts"] if p["quote_from"] <= read[-1]["index"]]
        text, mode = reading.quiz_source_text(read_text, read_parts, rd["compacts"])
        src = content["source"]
        material = {"id": src["material_id"], "title": src["material_title"], "unit": src["unit"]}
        label = generate.range_label(material["unit"], src["start"], src["end"])
        body = render(prompts(sid)["generate"], {
            "material_title": material["title"], "range_label": label, "range_text": text,
            "min_concepts": str(generate.MIN_CONCEPTS), "max_concepts": str(generate.MAX_CONCEPTS),
            "source_kind": "精簡筆記（原文已在導讀時讀過，這裡只保留出題所需的重點）" if mode == "compact" else "教材原文",
            "reading_log": reading.reading_log(read, rd) or "（沒有導讀紀錄）"})
        op = f"{sid}:generate:{upto}"
        try:
            gen, _ = await run_structured(ctx(state, "generate", f"{op}:{state.get('pending_event_id')}"), [HumanMessage(content=body)], generate.Generated,
                                          task="generate", node="generate", prompt_version=meta(r)["prompt_bundle"]["hash"],
                                          meta={"range_text": text, "reading_questions": [x["question"] for qs in rd["qa"].values() for x in qs]},
                                          source_ids=[q["unit_id"] for q in read], min_max_tokens=4096, min_context_tokens=16384)
        except TaskError as e:
            return {"error": model_error(e), "failed_node": "generate"}

        def mutate(c: dict, s: dict) -> None:
            generate.fill_content(c, gen)
            new = engine.initial_state(c)
            new["reading"] = s["reading"]
            new["reading"]["quiz_source"] = mode
            new["reading"]["quiz_scope"] = [q["unit_id"] for q in read]
            s.clear()
            s.update(new)

        repo.commit(sid, op + ":commit", kind="quiz", payload=gen.model_dump(), mutate=mutate)
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
        return {"ask": "ask", "next": "advance", "finish": "finish", "guide": "finalize"}[t]

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
    g.add_conditional_edges("wait_student", from_student, ["ask", "advance", "finish", "finalize"])
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
        task = guide_task(sid, nxt, 1, guides=rd["guides"])
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

    compiled.prefetch_next = prefetch_next   # used by the runner between commands
    return compiled


def initial_input(session_id: str, start_command_id: str) -> WorkflowState:
    return {"schema_version": STATE_SCHEMA_VERSION, "workflow_version": WORKFLOW_VERSION, "session_id": session_id,
            "stage": "segment", "pending_event": {"type": "start", "command_id": start_command_id, "payload": {}},
            "pending_event_id": start_command_id, "last_applied_event_id": None, "outcome": None, "error": None,
            "failed_node": None, "cmd_error": None, "result": None, "guide_attempt": {}, "todo_compact": [], "then": None}


def thread_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": f"session:{session_id}"}}
