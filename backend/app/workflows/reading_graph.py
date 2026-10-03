"""Guided-reading subgraph (plan §5.1): analyze → context → draft → validate → review → (revise → validate →
review) → accept | fail.

```mermaid
flowchart LR
    A[analyze] --> C[context] --> D[draft] --> V{validate}
    V -->|ok| R[review]
    V -->|major, can revise| X[revise]
    V -->|major, no budget| F[fail]
    R -->|pass| OK[accept]
    R -->|major, can revise| X
    R -->|"missing" claims after revision| Y[verify: extract the covering sentence]
    Y -->|all withdrawn| OK
    Y -->|still missing| F
    R -->|other major again| F
    X --> V
```

Every model call is its own node, so a checkpoint (durability="sync") follows each one. A node also stores its
state update as a `step` artifact under its operation key before returning, so a re-run after a crash between the
model reply and the checkpoint reuses the reply instead of calling again; the task's call count is rebuilt from the
llm_calls trace, so a restart never refills the budget. A connection failure raises (the graph stops at that node;
an explicit retry resumes there); invalid output after the allowed fix, an exhausted budget or an unresolved major
issue end in `fail`, which keeps the draft but never publishes it."""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from ..llm.runtime import CallBudget, ModelRuntime, TraceSink
from ..llm.tasks import TaskContext, TaskError, ToolDispatcher, run_structured, select_context
from ..reading import agent as ra
from ..reading.prompts import render
from ..reading.tools import ReadingScope, build_tools, tools_text


class ReadingUnavailable(Exception):
    """The model could not be reached; the subgraph stopped at the failing node and can be resumed."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class GuideState(TypedDict, total=False):
    task: dict
    op: str                      # operation key prefix of this attempt (session:command:guide:unit:attempt:version)
    command_id: str | None
    budget: dict
    plan: dict | None
    supplements: list[dict]
    draft: dict | None
    findings: list[dict]
    revised: bool
    decision: str | None
    status: str                  # running | accepted | failed
    error: dict | None
    artifact: dict | None
    history: Annotated[list, operator.add]


@dataclass
class ReadingDeps:
    runtime_for: Callable[[str], ModelRuntime]          # session id → runtime of its model snapshot
    prompts_for: Callable[[str], dict[str, str]]        # session id → prompt bundle texts
    scope_for: Callable[[str, int], ReadingScope]       # session id, unit index → read-only scope
    sink: TraceSink
    repo: Any = None                                    # Repository; None = no step cache (eval / unit tests)
    on_stage: Callable[[str, str | None, str], None] | None = None   # (session id, command id, stage) progress hook


def initial_state(task: ra.ReadingTaskInput, op: str, command_id: str | None = None) -> GuideState:
    return {"task": task.model_dump(), "op": op, "command_id": command_id,
            "budget": CallBudget.for_task("guide").to_dict(), "plan": None, "supplements": [], "draft": None,
            "findings": [], "revised": False, "decision": None, "status": "running", "error": None, "artifact": None,
            "history": []}


def build_reading_graph(deps: ReadingDeps, checkpointer=None):
    def ctx_for(state: GuideState, key: str) -> TaskContext:
        t = state["task"]
        budget = CallBudget.from_dict(state["budget"])
        if deps.repo is not None:   # calls made before a crash count, even if the checkpoint missed them
            budget.calls = max(budget.calls, deps.repo.count_calls(state["op"] + ":"))
        return TaskContext(deps.runtime_for(t["session_id"]), budget, deps.sink, session_id=t["session_id"],
                           command_id=state.get("command_id"), operation_key=key)

    def model_node(name: str, fn):
        async def node(state: GuideState) -> dict:
            key = f"{state['op']}:{name}:{'r' if state.get('revised') else 'a'}"
            if deps.repo is not None:
                cached = deps.repo.get_artifact(key)
                if cached:
                    return cached["payload"]
            if deps.on_stage:
                deps.on_stage(state["task"]["session_id"], state.get("command_id"), name)
            ctx = ctx_for(state, key)
            try:
                update = await fn(state, ctx)
            except TaskError as e:
                if e.kind == "unavailable":
                    raise ReadingUnavailable(e.detail) from e
                code = "budget_exhausted" if e.kind == "budget" else ("review_invalid" if name == "review" else "invalid_model_output")
                update = {"status": "failed", "error": {"code": code, "detail": f"{name}: {e.detail}"}}
            update["budget"] = ctx.budget.to_dict()
            if deps.repo is not None:
                deps.repo.put_artifact(key, kind="step", status="cached", payload=update,
                                       session_id=state["task"]["session_id"], unit_id=state["task"]["unit_id"])
            return update
        node.__name__ = name
        return node

    def prompts(state) -> dict[str, str]:
        return deps.prompts_for(state["task"]["session_id"])

    async def analyze(state, ctx):
        t = ra.ReadingTaskInput.model_validate(state["task"])
        msg = render(prompts(state)["reading_analyze"], ra.task_fields(t))
        plan, _ = await run_structured(ctx, [HumanMessage(content=msg)], ra.TeachingPlan, task="guide", node="analyze",
                                       prompt_version=t.prompt_bundle_hash, meta={"step": "analyze", "quote_text": t.text},
                                       source_ids=t.block_ids, min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        return {"plan": plan.model_dump()}

    async def context(state, ctx):
        t = ra.ReadingTaskInput.model_validate(state["task"])
        plan = ra.TeachingPlan.model_validate(state["plan"])
        if not ra.needs_context(t, plan):
            return {"supplements": []}
        tools = build_tools(deps.scope_for(t.session_id, t.index))
        fields = {**ra.task_fields(t), "needs_context": "\n".join(f"- {x}" for x in plan.needs_context) or "（原文以指涉語開頭）",
                  "tools": tools_text(tools), "max_calls": str(ctx.budget.max_tools_per_round)}
        msg = render(prompts(state)["reading_context"], fields)
        results, _ = await select_context(ctx, [HumanMessage(content=msg)], ToolDispatcher(tools, max_chars=3000),
                                          task="guide", node="context", prompt_version=t.prompt_bundle_hash,
                                          meta={"quote_text": t.text}, source_ids=t.block_ids)
        return {"supplements": [{"tool": r.tool, "args": r.args, "ok": r.ok, "content": r.content, "note": r.note}
                                for r in results]}

    def supplements_text(state) -> str:
        ok = [s for s in state.get("supplements") or [] if s["ok"]]
        return "\n\n".join(s["content"] for s in ok) or "（無）"

    async def draft(state, ctx):
        t = ra.ReadingTaskInput.model_validate(state["task"])
        fields = {**ra.task_fields(t), "teaching_plan": ra.plan_text(state.get("plan")), "supplements": supplements_text(state)}
        msg = render(prompts(state)["reading_draft"], fields)
        d, _ = await run_structured(ctx, [HumanMessage(content=msg)], ra.GuideDraft, task="guide", node="draft",
                                    prompt_version=t.prompt_bundle_hash, meta={"step": "draft", "quote_text": t.text},
                                    source_ids=t.block_ids, min_max_tokens=ra.GUIDE_MAX_TOKENS,
                                    min_context_tokens=ra.SUPPLEMENT_CONTEXT_TOKENS if state.get("supplements") else ra.GUIDE_CONTEXT_TOKENS)
        return {"draft": d.model_dump()}

    def validate(state: GuideState) -> dict:
        t = ra.ReadingTaskInput.model_validate(state["task"])
        fs = ra.validate_draft(t, ra.GuideDraft.model_validate(state["draft"]))
        budget = CallBudget.from_dict(state["budget"])
        # a revision needs its own call plus a re-review
        can_revise = budget.can("revision") and budget.calls + 2 <= budget.max_calls
        decision = ra.decide(fs, revised=state.get("revised", False), can_revise=can_revise)
        return {"findings": [f.model_dump() for f in fs], "decision": "review" if decision == "pass" else decision}

    async def review(state, ctx):
        t = ra.ReadingTaskInput.model_validate(state["task"])
        d = state["draft"]
        fields = {**ra.task_fields(t), "teaching_plan": ra.plan_text(state.get("plan")),
                  "chinese_text": d.get("chinese_text") or "（原文是中文，無）", "guide": d["guide"],
                  "supplement_notes": "\n".join(f"- {x}" for x in d.get("supplement_notes") or []) or "（無）"}
        msg = render(prompts(state)["reading_review"], fields)
        r, _ = await run_structured(ctx, [HumanMessage(content=msg)], ra.ReviewResult, task="guide", node="review",
                                    prompt_version=t.prompt_bundle_hash,
                                    meta={"step": "review", "quote_text": t.text, "draft": d["guide"]},
                                    # issues quote source and draft; 1024 tokens truncated the JSON on qwen3-vl:4b
                                    source_ids=t.block_ids, min_max_tokens=ra.GUIDE_MAX_TOKENS,
                                    min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        program = [ra.Finding.model_validate(f) for f in state.get("findings") or []]
        fs = program + ra.review_findings(r, d)
        can_revise = ctx.budget.can("revision") and ctx.budget.calls + 2 <= ctx.budget.max_calls
        decision = ra.decide(fs, revised=state.get("revised", False), can_revise=can_revise)
        # after the one revision, "missing" claims that still block are checked by extraction (one call)
        if decision == "fail" and ra.to_verify(fs) and ctx.budget.calls < ctx.budget.max_calls:
            decision = "verify"
        return {"findings": [f.model_dump() for f in fs], "decision": decision}

    async def verify(state, ctx):
        t = ra.ReadingTaskInput.model_validate(state["task"])
        d = state["draft"]
        fs = [ra.Finding.model_validate(f) for f in state["findings"]]
        idx = ra.to_verify(fs)
        claims = "\n".join(f"{k + 1}. {fs[i].source_excerpt or fs[i].detail}" for k, i in enumerate(idx))
        fields = {**ra.task_fields(t), "chinese_text": d.get("chinese_text") or "（原文是中文，無）", "guide": d["guide"],
                  "claims": claims}
        msg = render(prompts(state)["reading_verify"], fields)
        v, _ = await run_structured(ctx, [HumanMessage(content=msg)], ra.VerifyResult, task="guide", node="verify",
                                    prompt_version=t.prompt_bundle_hash, meta={"step": "verify", "quote_text": t.text},
                                    source_ids=t.block_ids, min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS,
                                    allow_fix=False)
        fs = ra.apply_verify(fs, idx, v, d)
        return {"findings": [f.model_dump() for f in fs],
                "decision": ra.decide(fs, revised=True, can_revise=False)}

    async def revise(state, ctx):
        t = ra.ReadingTaskInput.model_validate(state["task"])
        ctx.budget.charge("revision")
        majors = [ra.Finding.model_validate(f) for f in state["findings"] if f["severity"] == "major"]
        fields = {**ra.task_fields(t), "issues": ra.findings_text(majors), "draft_json": ra.draft_json(state["draft"])}
        msg = render(prompts(state)["reading_revise"], fields)
        d, _ = await run_structured(ctx, [HumanMessage(content=msg)], ra.GuideDraft, task="guide", node="revise",
                                    prompt_version=t.prompt_bundle_hash,
                                    meta={"step": "revise", "quote_text": t.text, "issues": [f.code for f in majors]},
                                    source_ids=t.block_ids, min_max_tokens=ra.GUIDE_MAX_TOKENS,
                                    min_context_tokens=ra.GUIDE_CONTEXT_TOKENS, purpose="revision")
        new = d.model_dump()
        if not (new.get("chinese_text") or "").strip() and (state["draft"].get("chinese_text") or "").strip():
            new["chinese_text"] = state["draft"]["chinese_text"]   # a revision fixes the issues, it does not drop the 中文課文
        return {"draft": new, "revised": True,
                "history": [{"draft": state["draft"], "findings": state["findings"]}]}

    def accept(state: GuideState) -> dict:
        t = state["task"]
        d = state["draft"]
        minors = [f for f in state.get("findings") or [] if f["severity"] == "minor"]
        b = state["budget"]
        refs = list(t["block_ids"]) + sorted({u for s in state.get("supplements") or [] if s["ok"]
                                               for u in (s["args"].get("unit_ids") or [s["args"].get("unit_id")]) if u})
        return {"status": "accepted", "artifact": {
            **{k: d.get(k) for k in ("chinese_text", "guide", "key_points", "check", "supplement_notes", "limits")},
            "unit_id": t["unit_id"], "source_refs": refs,
            "review_status": "passed_with_warnings" if minors else "passed", "warnings": minors,
            "revised": state.get("revised", False), "prompt_version": t["prompt_bundle_hash"],
            "model_config_hash": t["model_config_hash"], "workflow_version": ra.READING_WORKFLOW_VERSION,
            "calls": b["calls"], "attempt": t["attempt"], "agent": "langgraph"}}

    def fail(state: GuideState) -> dict:
        if state.get("status") == "failed" and state.get("error"):
            return {}
        majors = [f for f in state.get("findings") or [] if f["severity"] == "major"]
        return {"status": "failed", "error": {"code": "quality_check_failed",
                                              "detail": "導讀草稿沒有通過檢查：" + "；".join(f["detail"] for f in majors)[:400]}}

    g = StateGraph(GuideState)
    g.add_node("analyze", model_node("analyze", analyze))
    g.add_node("context", model_node("context", context))
    g.add_node("draft", model_node("draft", draft))
    g.add_node("validate", validate)
    g.add_node("review", model_node("review", review))
    g.add_node("revise", model_node("revise", revise))
    g.add_node("verify", model_node("verify", verify))
    g.add_node("accept", accept)
    g.add_node("fail", fail)

    def ok_or_fail(nxt: str):
        return lambda s: "fail" if s.get("status") == "failed" else nxt

    g.add_edge(START, "analyze")
    g.add_conditional_edges("analyze", ok_or_fail("context"), ["context", "fail"])
    g.add_conditional_edges("context", ok_or_fail("draft"), ["draft", "fail"])
    g.add_conditional_edges("draft", ok_or_fail("validate"), ["validate", "fail"])
    g.add_conditional_edges("validate", lambda s: s["decision"], {"review": "review", "revise": "revise", "fail": "fail"})
    g.add_conditional_edges("review", lambda s: "fail" if s.get("status") == "failed" else
                            {"pass": "accept", "revise": "revise", "fail": "fail", "verify": "verify"}[s["decision"]],
                            ["accept", "revise", "fail", "verify"])
    g.add_conditional_edges("verify", lambda s: "accept" if s.get("status") != "failed" and s["decision"] == "pass" else "fail",
                            ["accept", "fail"])
    g.add_conditional_edges("revise", ok_or_fail("validate"), ["validate", "fail"])
    g.add_edge("accept", END)
    g.add_edge("fail", END)
    return g.compile(checkpointer=checkpointer)
