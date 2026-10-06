"""Guided-reading subgraph v2 (R3): source first, then teaching.

```mermaid
flowchart TD
    P[prepare: program context + verified-source cache] -->|cache hit| B
    P -->|PDF symbols| SY[symbols: rendered regions read by the session's vision model] --> U
    P --> U[understand: claims + 中文課文]
    U -->|refers elsewhere| C[context: read-only tools] --> U
    U --> G1{gate1: anchors, full-text coverage, formulas, notation}
    G1 -->|audit needed| SA[source_audit] --> G1
    G1 -->|fixable| SR[revise_source] --> G1
    G1 -->|pass| B[plan_bridges: learning goal + approved bridges]
    B --> D[draft: 主要正文]
    D --> G2{gate2: body-only program checks}
    G2 --> BA[body_audit: every claim / bridge, full source] --> J{judge}
    G2 -->|program fact| BR
    J -->|pass| OK[accept]
    J -->|fixable| BR[revise_body] --> G2
    J -->|checkable claims| V[verify] --> J2{judge}
    J2 --> OK & BR
    G1 & J & J2 -->|evidence missing / budget / still failing| H[hold: draft kept, not published]
```

- Gate 1 must pass before any teaching text is written; Gate 2 must pass before the guide is published. The 中文課文
  is fixed by Gate 1 and never answers for the 主要正文.
- Every prompt is a ContextPacket: required items (the unit's source, the claims and bridges under check, the
  supplements the understanding used) are never cut; draft, audit, revision and re-check see the same source
  version and the same supplements. A request that cannot fit stops with `context_overflow`.
- A VerifiedSource is cached under everything it depends on (source text and blocks, context, symbol evidence,
  prompts, model, versions) and reused by later sessions when it holds no session data; bridges and the guide
  depend on the reader and are never shared.
- Every model call is its own node; its state update is stored as a `step` artifact under the operation key and the
  node's round, so a re-run after a crash reuses the reply; the call count is rebuilt from the trace, so a restart
  never refills the budget. One source revision, one body revision, one re-check per draft version."""
from __future__ import annotations

import json
import operator
from dataclasses import dataclass
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from ..llm.context import ContextItem, ContextOverflow, Packet
from ..llm.runtime import CallBudget, ModelRuntime, TraceSink, stable_hash
from ..llm.tasks import TaskContext, TaskError, ToolCallRequest, ToolDispatcher, run_structured, select_context
from ..reading import agent as ra
from ..reading import gates
from ..reading import pdf_evidence
from ..reading.contracts import (CONTRACT_VERSION, BodyAudit, BodyDraft, BridgePlan, GateCheck, GateResult,
                                 SourceAudit, SourceClaim, SourceUnderstanding, SymbolTranscription, VerifiedSource)
from ..reading.prompts import render
from ..reading.tools import ReadingScope, build_tools, tools_text
from . import faults


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
    gate_rules: str              # gates.GATE_RULES_VERSION this attempt runs under (checked before publishing)
    budget_base: int             # calls spent by earlier, discarded runs of this attempt (carried, never refilled)
    bridges_dropped: list        # bridges removed because they could not be verified (the guide fell back to the source)
    source_only: bool            # stop once the source is verified (warming the VerifiedSource cache ahead of the reader)
    rounds: dict                 # model node → times it has run (part of its step-cache key)
    supplements: list[dict]
    symbol_evidence: dict | None # R5: the unit's symbol evidence, confirmed by the symbols node when possible
    context_status: str          # not_needed | provided | outside_range | missing
    model_context_done: bool
    understanding: dict | None
    gate1: dict | None           # GateResult of the current understanding
    audited: bool                # the current understanding went through source_audit
    source_revisions: int
    verified: dict | None        # VerifiedSource
    bridges: dict | None         # BridgePlan
    draft: dict | None           # BodyDraft
    body_revisions: int
    verified_for: int | None     # body revision count a focused re-check already ran on
    gate2: dict | None
    audit: dict | None
    findings: list[dict]
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
    vsrc_cache: dict | None = None                      # in-memory verified-source cache when there is no repo
    render_region: Callable | None = None               # (pdf_path, page, bbox) → PNG bytes; default: pdf_evidence
    evidence_dir: Any = None                            # where rendered regions are kept (Path); None = not kept


def initial_state(task: ra.ReadingTaskInput, op: str, command_id: str | None = None,
                  budget: dict | None = None, source_only: bool = False) -> GuideState:
    """`budget`: what an earlier, discarded run of the same attempt already spent (a run restarted because the gate
    rules changed keeps its spending; the budget is never refilled)."""
    return {"task": task.model_dump(), "op": op, "command_id": command_id, "gate_rules": gates.GATE_RULES_VERSION,
            "source_only": source_only,
            "budget": budget or CallBudget.for_task("guide").to_dict(), "budget_base": int((budget or {}).get("calls") or 0),
            "rounds": {}, "supplements": [], "symbol_evidence": task.symbol_evidence, "context_status": "not_needed",
            "model_context_done": False,
            "understanding": None, "gate1": None, "audited": False, "source_revisions": 0, "verified": None,
            "bridges": None, "draft": None, "body_revisions": 0, "verified_for": None, "gate2": None, "audit": None,
            "findings": [], "decision": None, "status": "running", "error": None, "artifact": None, "history": []}


SOURCE_PROMPTS = ("reading_understand", "reading_source_audit", "reading_source_revise")


def understanding_tokens(t: ra.ReadingTaskInput) -> int:
    """Output reserved for claims (+ the 中文課文 of a non-Chinese source): about three times the source, 2048–4096."""
    from ..llm.context import estimate_tokens
    return max(2048, min(4096, 3 * estimate_tokens(t.text) + 512))


def template(lists: dict[str, list[str]], item: dict, rest: dict) -> str:
    """An output skeleton naming every id the model must report on (small models follow a filled-in shape)."""
    return json.dumps({**{k: [{"id": i, **item[k]} for i in ids] for k, ids in lists.items()}, **rest}, ensure_ascii=False)
SUPPORT_SKELETON = {"source": "S…", "derivation": "前提 S…；推導：…", "prior_reading": "單元:C…", "user_authorized": "讀者說「…」",
                    "prerequisite": "…"}
# Gate 2 findings that concern only the optional teaching added to the source; when nothing else blocks, the guide
# falls back to the source alone instead of being held (bridges are never required, unverified ones never published)
BRIDGE_CODES = {"bridge_missing", "bridge_incorrect", "bridge_unconfirmed", "bridge_correctness_unconfirmed",
                "unapproved_supplement"}
MAX_SOURCE_REVISIONS = 1
MAX_BODY_REVISIONS = 1


def claims_text(claims: list[dict], with_quote: bool = True) -> str:
    lines = []
    for c in claims:
        extra = [f"條件：{'；'.join(c['conditions'])}" if c.get("conditions") else "",
                 f"例外：{'；'.join(c['exceptions'])}" if c.get("exceptions") else "",
                 f"力度：{c['strength']}" if c.get("strength") else "",
                 f"公式：{'；'.join(c['formulas'])}" if c.get("formulas") else ""]
        extra = "；".join(x for x in extra if x)
        lines.append(f"- {c['id']}［{c.get('function', 'claim')}］{c['statement']}" + (f"（{extra}）" if extra else "")
                     + (f"\n  原文（{'、'.join(c.get('sentences') or [])}）：「{c.get('source_text') or c.get('quote') or ''}」"
                        if with_quote else ""))
    return "\n".join(lines) or "（無）"


def bridges_text(plan: dict | None) -> str:
    bs = (plan or {}).get("bridges") or []
    if not bs:
        return "（沒有核准的橋梁：原文以外不要加入任何內容）"
    return "\n".join(f"- {b['id']}［{'補充說明' if b['boundary'] == 'supplement' else '融入正文'}｜依據 {b['basis']}｜服務 "
                     f"{'、'.join(b['targets'])}］{b['content']}" for b in bs)


def build_reading_graph(deps: ReadingDeps, checkpointer=None):
    def spent_calls(state) -> int:
        """Calls this attempt has made: what earlier, discarded runs spent (budget_base, fixed when the run starts)
        plus every call recorded under this run's operation key — failed calls included, even those the checkpoint
        never saw. The two never overlap: each run has its own operation key."""
        return int(state.get("budget_base") or 0) + deps.repo.count_calls(state["op"] + ":")

    def ctx_for(state: GuideState, key: str) -> TaskContext:
        t = state["task"]
        budget = CallBudget.from_dict(state["budget"])
        if deps.repo is not None:   # calls made before a crash count, even if the checkpoint missed them
            budget.calls = max(budget.calls, spent_calls(state))
        return TaskContext(deps.runtime_for(t["session_id"]), budget, deps.sink, session_id=t["session_id"],
                           command_id=state.get("command_id"), operation_key=key)

    def model_node(name: str, fn):
        async def node(state: GuideState) -> dict:
            rounds = dict(state.get("rounds") or {})
            key = f"{state['op']}:{name}:{rounds.get(name, 0)}"
            if deps.repo is not None:
                cached = deps.repo.get_artifact(key)
                if cached:
                    return cached["payload"]
            if deps.on_stage:
                deps.on_stage(state["task"]["session_id"], state.get("command_id"), name)
            faults.hit(f"before:{name}")
            ctx = ctx_for(state, key)
            try:
                update = await fn(state, ctx)
            except TaskError as e:
                if e.kind == "unavailable":
                    raise ReadingUnavailable(e.detail) from e
                code = {"budget": "budget_exhausted", "context_overflow": "context_overflow"}.get(
                    e.kind, "review_invalid" if name in ("source_audit", "body_audit") else "invalid_model_output")
                update = {"status": "failed", "error": {"code": code, "detail": f"{name}: {e.detail}"}}
            update["budget"] = ctx.budget.to_dict()
            update["rounds"] = {**rounds, name: rounds.get(name, 0) + 1}
            if deps.repo is not None:
                deps.repo.put_artifact(key, kind="step", status="cached", payload=update,
                                       session_id=state["task"]["session_id"], unit_id=state["task"]["unit_id"])
            return update
        node.__name__ = name
        return node

    def prompts(state) -> dict[str, str]:
        return deps.prompts_for(state["task"]["session_id"])

    def task(state) -> ra.ReadingTaskInput:
        return ra.ReadingTaskInput.model_validate(state["task"])

    def can(budget: CallBudget, cost: int) -> bool:
        return budget.calls + cost <= budget.max_calls

    # -- packets ------------------------------------------------------------------------------
    def supplements_text(state) -> str:
        ok = [s for s in state.get("supplements") or [] if s["ok"]]
        text = "\n\n".join(s["content"] + ("" if s.get("complete", True) else f"\n（部分結果；{s.get('note')}）") for s in ok)
        if state.get("context_status") == "outside_range":
            text += ("\n\n" if text else "") + "（這一段指涉的前文不在本次學習範圍內，無法提供）"
        return text or "（無）"

    def supplement_ids(state) -> list[str]:
        return sorted({u for s in state.get("supplements") or [] if s["ok"] for u in s.get("source_ids") or []})

    def items_for(state, t: ra.ReadingTaskInput, keys: tuple[str, ...]) -> list[ContextItem]:
        """The context items of one prompt. Required: the source, claims, bridges and the supplements the
        understanding relied on; optional (dropped first when the call does not fit): what the reader read and
        asked before, the 中文課文 as a reading aid."""
        relied = state.get("context_status") == "provided"
        learning = t.learning
        verified = state.get("verified") or {}
        bridges = (state.get("bridges") or {}).get("bridges") or []
        make = {
            "symbol_evidence": lambda: ContextItem(key="symbol_evidence", text=symbol_text(state.get("symbol_evidence")),
                                                   version=(state.get("symbol_evidence") or {}).get("version")),
            "sentences": lambda: ContextItem(key="sentences", text=gates.numbered_source(t.text),
                                             source_ids=t.block_ids, version=t.source_hash),
            "quote_text": lambda: ContextItem(key="quote_text", text=t.text + (
                "\n\n（注意：這一段含有圖，但圖片內容沒有被抽取出來，只能依文字說明。）" if t.has_figure else ""),
                source_ids=t.block_ids, version=t.source_hash),
            # what the 主要正文 is written from. A non-Chinese source is given as its audited 中文課文 only: shown the
            # English, qwen3-vl:4b pasted it into the guide again and again, revisions included
            "body_source": lambda: ContextItem(key="quote_text", text=body_source_text(t, verified),
                                               source_ids=t.block_ids, version=t.source_hash),
            "supplements": lambda: ContextItem(key="supplements", text=supplements_text(state), required=relied, priority=3,
                                               source_ids=supplement_ids(state),
                                               complete=all(s.get("complete", True) for s in state.get("supplements") or [])),
            "claims": lambda: ContextItem(key="claims", text=claims_text(verified.get("claims") or []),
                                          source_ids=[c["id"] for c in verified.get("claims") or []], version=verified.get("key")),
            "bridges": lambda: ContextItem(key="bridges", text=bridges_text(state.get("bridges")),
                                           source_ids=[b["id"] for b in bridges]),
            "chinese_text": lambda: ContextItem(key="chinese_text", text=verified.get("chinese_text") or "（原文是中文，無）",
                                                required=False, priority=4, version=verified.get("key")),
            "prev_points": lambda: ContextItem(key="prev_points", text="\n".join(f"- {p}" for p in t.prev_points) or "（這是第一段）",
                                               required=False, priority=2),
            "qa_context": lambda: ContextItem(key="qa_context", text=ra.qa_text(t.qa_context), required=False, priority=1),
            "read_claims": lambda: ContextItem(key="read_claims", text="\n".join(
                f"  - [{x['unit_id']}] {x['statement']}" for x in learning.read_claims) or "  （尚未讀過其他段落）",
                required=False, priority=2, source_ids=[f"{x['unit_id']}:{x['claim_id']}" for x in learning.read_claims]),
            "bridge_refs": lambda: ContextItem(key="bridge_refs", text=bridge_refs_text(learning), required=False, priority=2,
                                               source_ids=[f"{x['unit_id']}:{x['claim_id']}" for x in learning.read_claims]),
            "topic_context": lambda: ContextItem(key="topic_context", text=topic_text(t), required=False, priority=3),
            "open_questions": lambda: ContextItem(key="open_questions", text="\n".join(
                f"  - [{x['unit_id']}] {x['question']}" for x in learning.open_questions) or "  （無）",
                required=False, priority=1),
        }
        return [make[k]() for k in keys]

    def body_source_text(t: ra.ReadingTaskInput, verified: dict) -> str:
        figure = "\n\n（注意：這一段含有圖，但圖片內容沒有被抽取出來，只能依文字說明。）" if t.has_figure else ""
        zh = verified.get("chinese_text")
        if ra.is_chinese(t.text) or not zh:
            return t.text + figure
        return ("（原文不是中文。以下是逐句核對過的中文課文，內容、條件與公式都與原文一致；正文依此用繁體中文撰寫，"
                "不要寫成原文的語言。）\n\n" + zh + figure)

    def topic_text(t: ra.ReadingTaskInput) -> str:
        """Where this 區塊 sits in its 主題: the 主題總摘要 and every 區塊's outline, this one marked. Scope only."""
        tp = t.topic or {}
        chunks = tp.get("chunks") or []
        if not chunks:
            return "（這一段就是一個完整主題，沒有分區塊）"
        k = int(tp.get("index", 0))
        lines = [f"主題總摘要：{tp.get('summary') or '（無）'}",
                 f"這個主題分成 {len(chunks)} 個區塊，你現在只寫第 {k + 1} 塊（標 ▶）："]
        for j, c in enumerate(chunks):
            lines.append(f"{'▶' if j == k else '  '} 第 {j + 1} 塊｜{c.get('topic') or '—'}｜{c.get('aspect') or '—'}"
                         f"｜{c.get('summary') or '—'}")
        lines.append(f"只寫第 {k + 1} 塊原文裡的內容；前面的區塊學生已經讀過，後面的區塊會另外導讀，都不要寫進來。")
        return "\n".join(lines)

    def bridge_refs(t: ra.ReadingTaskInput) -> dict:
        """What a non-source bridge may cite as checkable support (see gates.bridge_support_problem)."""
        lr = t.learning
        return {"read": {f"{x['unit_id']}:{x['claim_id']}" for x in lr.read_claims},
                "reader": "\n".join(x for x in (lr.goal, lr.background) if x)}

    def bridge_refs_text(lr) -> str:
        read = "\n".join(f"  - {x['unit_id']}:{x['claim_id']} {x['statement']}" for x in lr.read_claims) or "  （無）"
        reader = "\n".join(f"  - {x}" for x in (lr.goal, lr.background) if x) or "  （讀者沒有提出）"
        return f"讀者已讀的命題（依據為已讀段落時，引用「單元:命題編號」）：\n{read}\n讀者提出的要求與背景（依據為讀者授權時，用「」引用原話）：\n{reader}"

    def with_optional(template_name: str, state, keys: tuple[str, ...], optional: tuple[str, ...]) -> tuple[str, ...]:
        """Items a session pinned to an older prompt bundle has no placeholder for are not sent."""
        tmpl = prompts(state)[template_name]
        return keys + tuple(k for k in optional if "{{" + k + "}}" in tmpl)

    def symbol_text(ev: dict | None) -> str:
        if not ev or ev.get("status") != "confirmed":
            return "（這一段沒有需要另外取證的符號；公式照原文的 LaTeX 寫）"
        tr = ev.get("transcription") or {"formulas": [], "symbols": []}
        lines = ["依原書版面的視覺取證（以此為準，不要依文字層或常識改寫）："]
        lines += [f"- 公式：${f}$" for f in tr["formulas"]]
        lines += [f"- {x['symbol']}：{x['latex']}（{x['style']}{'' if x['script'] == 'none' else '，' + x['script']}）"
                  for x in tr["symbols"]]
        lines += [f"- 取證區域：第 {r['page']} 頁 bbox {r['bbox']}" + (f"（截圖 {r['png']}）" if r.get("png") else "")
                  for r in ev.get("renders") or ev.get("regions") or []]
        lines.append("上列每個公式都必須照寫在命題與正文中。")
        return "\n".join(lines)

    def fixed_fields(t: ra.ReadingTaskInput) -> dict[str, str]:
        f = ra.task_fields(t)
        return {k: f[k] for k in ("material_title", "range_label", "position", "quote_label", "heading", "unit_id")} | {
            "chinese_text_rule": ra.understanding_cn_rule(t.text),
            "depth": "略讀（skim）" if t.learning.depth == "skim" else "細讀（close）",
            "goal": t.learning.goal or "（未指定）",
            "background": t.learning.background or "（讀者沒有提供）"}

    async def prompt_for(ctx: TaskContext, state, node: str, template: str, keys: tuple[str, ...], extra: dict[str, str],
                         max_tokens: int):
        t = task(state)
        limits = await ctx.runtime.limits(ra.GUIDE_CONTEXT_TOKENS)
        try:
            return Packet(node, prompts(state)[template], items_for(state, t, keys), {**fixed_fields(t), **extra}).build(
                ctx.runtime.max_tokens(max_tokens), limits)
        except ContextOverflow as e:
            raise TaskError("context_overflow", e.detail) from e

    # -- verified-source cache ----------------------------------------------------------------
    def vsrc_key(state) -> tuple[str, bool]:
        t = task(state)
        shareable = all(s.get("tool") in ("read_source_units", "read_neighbor_units") for s in state.get("supplements") or [])
        p = prompts(state)
        key = "vsrc:" + stable_hash([t.text, t.block_ids, t.source_kind, t.symbol_evidence,
                                     [s["content"] for s in state.get("supplements") or [] if s["ok"]],
                                     {k: p.get(k) for k in SOURCE_PROMPTS}, t.model_config_hash,
                                     ra.READING_WORKFLOW_VERSION, CONTRACT_VERSION, gates.GATE_RULES_VERSION,
                                     None if shareable else t.session_id], 24)
        return key, shareable

    def vsrc_get(key: str) -> dict | None:
        """A cached verification counts only if it passed the current Gate 1 rules (an entry written by older rules,
        or one whose rules field does not match, is verified again)."""
        if deps.repo is not None:
            a = deps.repo.get_artifact(key)
            v = a["payload"] if a else None
        else:
            v = (deps.vsrc_cache or {}).get(key)
        return v if v and v.get("verifier_version") == gates.GATE_RULES_VERSION else None

    def vsrc_put(key: str, v: dict, t: ra.ReadingTaskInput, shareable: bool) -> None:
        if deps.repo is not None:
            deps.repo.put_artifact(key, kind="verified_source", status="accepted", payload=v,
                                   session_id=None if shareable else t.session_id, unit_id=t.unit_id,
                                   source_hash=t.source_hash, prompt_version=t.prompt_bundle_hash,
                                   model_config_hash=t.model_config_hash, workflow_version=ra.READING_WORKFLOW_VERSION)
        elif deps.vsrc_cache is not None:
            deps.vsrc_cache[key] = v

    # -- nodes ------------------------------------------------------------------------------------
    async def prepare(state: GuideState) -> dict:
        """Program-side context: a unit that opens by pointing back ("在這兩個條件下…") gets its previous unit,
        read through the same read-only tool and recorded the same way; no model call is needed for that."""
        t = task(state)
        if deps.repo is not None:
            # the run's input, so that scripts/gate_cases.py can replay it later under other gate rules (its model
            # calls are in llm_calls under the same operation key); first write wins on a resume
            deps.repo.put_artifact(f"{state['op']}:input", kind="guide_input", status="cached", payload=dict(state),
                                   session_id=t.session_id, unit_id=t.unit_id)
        sups, status = [], "not_needed"
        tp = t.topic or {}
        if (ra.REFERS_BACK_RE.match(t.text) or t.depends_on_prev) and int(tp.get("index", 0)) > 0:
            # a later 區塊 points back into its own 主題: the previous 區塊, given by the program
            prev = tp.get("prev") or {}
            sups.append({"tool": "read_neighbor_units", "args": {"unit_id": tp.get("unit_id"), "block": tp["index"]},
                         "ok": bool(prev.get("text")), "content": prev.get("text") or "", "note": "同一主題的前一個區塊",
                         "source_ids": prev.get("block_ids") or [], "version": "", "complete": True, "omitted": [],
                         "by": "program"})
            status = "provided" if prev.get("text") else "missing"
        elif ra.REFERS_BACK_RE.match(t.text) or t.depends_on_prev:
            if t.index == 0:
                status = "outside_range"
            else:
                d = ToolDispatcher(build_tools(deps.scope_for(t.session_id, t.index)))
                res = await d.run([ToolCallRequest(tool="read_neighbor_units",
                                                   args={"unit_id": tp.get("unit_id") or t.unit_id, "before": 1,
                                                         "after": 0})], 1)
                sups += [{**r.as_dict(), "by": "program"} for r in res]
                status = "provided" if any(r.ok for r in res) else "missing"
        key, _ = vsrc_key({**state, "supplements": sups})
        cached = vsrc_get(key)
        return {"supplements": sups, "context_status": status, "verified": cached,
                "gate1": cached["gate1"] if cached else None}

    def needs_symbols(state) -> bool:
        t = task(state)
        ev = state.get("symbol_evidence")
        if t.source_kind != "pdf":
            return False
        return (ev or {}).get("status") in ("needs_visual", "unavailable") or (ev is None and gates.symbol_like(t.text))

    async def symbols(state, ctx):
        """Read the unit's rendered PDF regions with the session's own model, if it can take images. Missing
        evidence or a text-only model ends here (no understanding is written that could not pass Gate 1)."""
        t = task(state)
        ev = dict(state.get("symbol_evidence") or {"status": "unavailable", "regions": [],
                                                   "reason": "這一段有數學符號，但沒有原始 PDF 的版面證據，無法確認符號的字型"})
        caps = await ctx.runtime.backend.capabilities()
        if ev["status"] == "unavailable" or not caps.vision:
            reason = ev.get("reason") if ev["status"] == "unavailable" else \
                f"{ev.get('reason')}；目前的模型（{ctx.runtime.backend.name} {ctx.runtime.backend.model}）不支援影像輸入，無法看圖核對"
            return {"symbol_evidence": {**ev, "status": "unverified", "reason": reason},
                    "status": "failed", "error": {"code": "notation_unverified", "detail": reason + "（位置：" + "、".join(
                        f"第 {r['page']} 頁" for r in ev.get("regions", [])) + "）"}}
        render = deps.render_region or pdf_evidence.render_region
        import base64
        parts: list = [{"type": "text", "text": ""}]
        renders = []
        for r in ev["regions"][:4]:
            png = render(ev["pdf_path"], r["page"], r["bbox"])
            renders.append({**r, "png": pdf_evidence.png_hash(png)})
            if deps.evidence_dir is not None:
                from pathlib import Path
                Path(deps.evidence_dir).mkdir(parents=True, exist_ok=True)
                (Path(deps.evidence_dir) / f"{renders[-1]['png']}.png").write_bytes(png)
            parts.append({"type": "image", "base64": base64.b64encode(png).decode(), "mime_type": "image/png"})
        clues = ev.get("clues") or {}
        extra = {"clues": "\n".join(f"- {k}：{v}" for k, v in clues.items() if v) or "（無）"}
        limits = await ctx.runtime.limits(ra.GUIDE_CONTEXT_TOKENS)
        msg, rec = Packet("symbols", prompts(state)["reading_symbols"], items_for(state, t, ("quote_text",)),
                          {**fixed_fields(t), **extra}).build(ctx.runtime.max_tokens(1024), limits)
        parts[0]["text"] = msg
        tr, _ = await run_structured(ctx, [HumanMessage(content=parts)], SymbolTranscription, task="guide", node="symbols",
                                     prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                     meta={"step": "symbols", "quote_text": t.text, "regions": ev["regions"]},
                                     min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        problems = gates.transcription_against_clues(tr, clues)
        if problems:
            reason = "PDF 符號的視覺判讀與字型線索不一致：" + "；".join(problems)
            return {"symbol_evidence": {**ev, "status": "unverified", "reason": reason, "transcription": tr.model_dump(),
                                        "renders": renders},
                    "status": "failed", "error": {"code": "notation_unverified", "detail": reason}}
        summary = "；".join(f"{x.symbol}={x.latex}" for x in tr.symbols)[:200]
        return {"symbol_evidence": {**ev, "status": "confirmed", "transcription": tr.model_dump(), "renders": renders,
                                    "summary": summary, "model": f"{ctx.runtime.backend.name}:{ctx.runtime.backend.model}"}}

    async def understand(state, ctx):
        t = task(state)
        out_tokens = understanding_tokens(t)
        msg, rec = await prompt_for(ctx, state, "understand", "reading_understand",
                                    ("sentences", "symbol_evidence", "supplements"), {}, out_tokens)
        u, _ = await run_structured(ctx, [HumanMessage(content=msg)], SourceUnderstanding, task="guide", node="understand",
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    json_schema=gates.understanding_schema(t.text),
                                    meta={"step": "understand", "quote_text": t.text,
                                          "segments": gates.source_segments(t.text),
                                          "evidence_formulas": gates.evidence_formulas(state.get("symbol_evidence"))},
                                    min_max_tokens=out_tokens, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        return {"understanding": gates.with_chinese_text(t.text, u).model_dump(), "audited": False, "gate1": None}

    def wants_model_context(state) -> bool:
        u = state.get("understanding") or {}
        return (not state.get("model_context_done") and state.get("context_status") != "provided"
                and any(ra.CONTEXT_HINT_RE.search(x) for x in u.get("needs_context") or []))

    async def context(state, ctx):
        t = task(state)
        u = state["understanding"]
        tools = build_tools(deps.scope_for(t.session_id, t.index))
        fields = {**ra.task_fields(t), "needs_context": "\n".join(f"- {x}" for x in u.get("needs_context") or []),
                  "tools": tools_text(tools), "max_calls": str(ctx.budget.max_tools_per_round)}
        msg = render(prompts(state)["reading_context"], fields)
        results, _ = await select_context(ctx, [HumanMessage(content=msg)], ToolDispatcher(tools), task="guide",
                                          node="context", prompt_version=t.prompt_bundle_hash,
                                          meta={"quote_text": t.text}, source_ids=t.block_ids)
        got = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok and r.note.startswith(("工具拒絕", "未提供"))]
        status = "provided" if got else ("missing" if failed else state.get("context_status"))
        upd = {"supplements": (state.get("supplements") or []) + [{**r.as_dict(), "by": "model"} for r in results],
               "model_context_done": True, "context_status": status}
        if got:   # the understanding was made without this context: it is redone with it (same budget)
            upd["understanding"] = None
        return upd

    def gate1(state: GuideState) -> dict:
        t = task(state)
        u = SourceUnderstanding.model_validate(state["understanding"])
        if state.get("audited") and state.get("gate1"):   # the audit already merged into this understanding's checks
            checks = [GateCheck.model_validate(c) for c in state["gate1"]["checks"]]
        else:
            checks = gates.gate1_program(t.text, u, source_kind=t.source_kind, symbol_evidence=state.get("symbol_evidence"))
            if state.get("context_status") == "missing":
                checks.append(GateCheck(id="context", kind="context", status="unverified", fixable=False,
                                        detail="這一段需要的前後文讀取失敗，無法確認指涉"))
        budget = CallBudget.from_dict(state["budget"])
        decision = gates.gate1_decide(checks, audited=state.get("audited", False),
                                      need_audit=gates.needs_source_audit(t.text, checks), can_audit=can(budget, 1),
                                      can_revise=state.get("source_revisions", 0) < MAX_SOURCE_REVISIONS and can(budget, 2))
        result = GateResult.of("gate1", checks)
        upd: dict = {"gate1": result.model_dump(), "decision": decision}
        if decision == "pass":
            key, shareable = vsrc_key(state)
            v = VerifiedSource(key=key, unit_id=t.unit_id, source_hash=t.source_hash, chinese_text=u.chinese_text,
                               claims=gates.resolve_claims(t.text, u.claims, u.translations), gate1=result, supplement_ids=supplement_ids(state), shareable=shareable,
                               limits=u.limits, verifier_version=gates.GATE_RULES_VERSION).model_dump()
            vsrc_put(key, v, t, shareable)
            upd["verified"] = v
        return upd

    async def source_audit(state, ctx):
        t = task(state)
        u = SourceUnderstanding.model_validate(state["understanding"])
        checks = [GateCheck.model_validate(c) for c in state["gate1"]["checks"]]
        sentences, claims = gates.audit_items(t.text, u, checks)
        ids = {"sentences": [sid for sid, _ in sentences], "claims": [c.id for c in claims]}
        tr = {x.id: x.zh for x in u.translations}
        extra = {"chinese_text": u.chinese_text or "（原文是中文，無）",
                 "audit_sentences": "\n".join(f"- {sid}：{s}\n  譯文：{tr.get(sid, '（缺）')}" for sid, s in sentences) or "（無）",
                 "claims": claims_text([c.model_dump() for c in claims]),
                 "output_template": template(ids, {"sentences": {"verdict": "faithful", "note": ""},
                                                   "claims": {"verdict": "faithful", "note": ""}}, {})}
        msg, rec = await prompt_for(ctx, state, "source_audit", "reading_source_audit", ("quote_text",), extra, 2048)
        a, _ = await run_structured(ctx, [HumanMessage(content=msg)], SourceAudit, task="guide", node="source_audit",
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    check=gates.audit_check(sentences, claims),
                                    json_schema=gates.exact_ids_schema(SourceAudit, ids),
                                    meta={"step": "source_audit", "quote_text": t.text, "sentences": sentences,
                                          "claim_ids": [c.id for c in claims]},
                                    min_max_tokens=2048, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        merged = gates.gate1_apply_audit(t.text, u, checks, a, sentences)
        return {"gate1": GateResult.of("gate1", merged).model_dump(), "audited": True}

    async def revise_source(state, ctx):
        t = task(state)
        ctx.budget.charge("revision")
        failing = [c for c in state["gate1"]["checks"] if c["status"] in ("fail", "unverified")]
        extra = {"issues": "\n".join(f"- [{c['kind']}] {c['detail']}" for c in failing),
                 "understanding_json": json.dumps(state["understanding"], ensure_ascii=False)}
        out_tokens = understanding_tokens(t)
        msg, rec = await prompt_for(ctx, state, "revise_source", "reading_source_revise",
                                    ("sentences", "symbol_evidence", "supplements"), extra, out_tokens)
        u, _ = await run_structured(ctx, [HumanMessage(content=msg)], SourceUnderstanding, task="guide",
                                    node="revise_source", prompt_version=t.prompt_bundle_hash, packet=rec,
                                    source_ids=t.block_ids, purpose="revision", json_schema=gates.understanding_schema(t.text),
                                    meta={"step": "revise_source", "quote_text": t.text,
                                          "segments": gates.source_segments(t.text),
                                          "evidence_formulas": gates.evidence_formulas(state.get("symbol_evidence"))},
                                    min_max_tokens=out_tokens, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        return {"understanding": gates.with_chinese_text(t.text, u).model_dump(), "audited": False, "gate1": None,
                "source_revisions": state.get("source_revisions", 0) + 1,
                "history": [{"stage": "source", "understanding": state["understanding"], "gate1": state["gate1"]}]}

    async def plan_bridges(state, ctx):
        t = task(state)
        v = state["verified"]
        msg, rec = await prompt_for(ctx, state, "plan_bridges", "reading_bridges",
                                    with_optional("reading_bridges", state,
                                                  ("quote_text", "claims", "read_claims", "open_questions"),
                                                  ("topic_context",)), {}, 1024)
        claim_ids = {c["id"] for c in v["claims"]}
        b, _ = await run_structured(ctx, [HumanMessage(content=msg)], BridgePlan, task="guide", node="plan_bridges",
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    check=gates.bridge_check(claim_ids, bool(t.learning.read_claims)),
                                    meta={"step": "plan_bridges", "quote_text": t.text, "claim_ids": sorted(claim_ids)},
                                    min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        return {"bridges": b.model_dump()}

    async def draft(state, ctx):
        t = task(state)
        n_bridges = len((state.get("bridges") or {}).get("bridges") or [])
        out_tokens = gates.body_tokens(t.text, n_bridges)
        msg, rec = await prompt_for(ctx, state, "draft", "reading_body",
                                    with_optional("reading_body", state,
                                                  ("body_source", "symbol_evidence", "claims", "bridges", "chinese_text",
                                                   "supplements", "prev_points", "qa_context"), ("topic_context",)),
                                    {}, out_tokens)
        d, _ = await run_structured(ctx, [HumanMessage(content=msg)], BodyDraft, task="guide", prose_fields=("guide",), node="draft",
                                    json_schema=gates.body_schema(t.text, n_bridges),
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    meta={"step": "draft", "quote_text": t.text, "claims": state["verified"]["claims"],
                                          "bridges": (state.get("bridges") or {}).get("bridges") or [],
                                          "evidence_formulas": gates.evidence_formulas(state.get("symbol_evidence"))},
                                    min_max_tokens=out_tokens, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        return {"draft": d.model_dump()}

    def claims_of(state) -> list[SourceClaim]:
        return [SourceClaim.model_validate(c) for c in state["verified"]["claims"]]

    def gate2(state: GuideState) -> dict:
        """Program checks of the 主要正文 alone; a confirmed program fact goes straight to revision."""
        t = task(state)
        d = state["draft"]
        fs = gates.gate2_program(t.text, d["guide"], claims_of(state), BridgePlan.model_validate(state.get("bridges") or {}),
                                 d.get("bridges_used") or [], t, symbol_evidence=state.get("symbol_evidence"))
        confirmed = [f for f in fs if f.severity == "major" and f.status == "confirmed"]
        budget = CallBudget.from_dict(state["budget"])
        decision = "audit"
        if confirmed:
            decision = "revise" if state.get("body_revisions", 0) < MAX_BODY_REVISIONS and can(budget, 2) else "fail"
        return {"findings": [f.model_dump() for f in fs], "decision": fallback(state, budget, fs, decision)}

    def fallback(state, budget: CallBudget, fs: list[ra.Finding], decision: str) -> str:
        """A guide about to be held only for its teaching additions is redrafted once from the source alone."""
        majors = [f for f in fs if f.severity == "major"]
        if (decision == "fail" and majors and all(f.code in BRIDGE_CODES for f in majors)
                and state.get("bridges_dropped") is None and can(budget, 2)):
            return "drop_bridges"
        return decision

    def judged(state, budget: CallBudget, fs: list[ra.Finding], *, allow_verify: bool) -> str:
        revs = state.get("body_revisions", 0)
        return fallback(state, budget, fs, ra.decide(fs, revised=revs >= MAX_BODY_REVISIONS, can_revise=can(budget, 2),
                                                     can_verify=allow_verify and can(budget, 1) and state.get("verified_for") != revs))

    def drop_bridges(state: GuideState) -> dict:
        """Remove every bridge (and any 補充說明) and write the guide again from the verified source alone; the
        dropped bridges and why they could not be verified stay in the record. Spending is kept (one budget)."""
        plan = BridgePlan.model_validate(state.get("bridges") or {})
        why = [f["detail"][:160] for f in state.get("findings") or [] if f["severity"] == "major"]
        dropped = [{"id": b.id, "content": b.content, "basis": b.basis} for b in plan.bridges]
        return {"bridges": BridgePlan(learning_goal=plan.learning_goal, bridges=[]).model_dump(),
                "bridges_dropped": [{"bridges": dropped, "reasons": why}],
                "draft": None, "findings": [], "audit": None, "decision": None, "body_revisions": 0, "verified_for": None,
                "history": [{"stage": "drop_bridges", "draft": state["draft"], "findings": state["findings"]}]}

    async def body_audit(state, ctx):
        t = task(state)
        d = state["draft"]
        # claims the guide writes out verbatim are covered by program fact; only the rest go to the auditor
        all_ids = [c["id"] for c in state["verified"]["claims"]]
        claims = [c for c in claims_of(state) if not gates.verbatim_covered(c, d["guide"], t.text)]
        audited = {c.id for c in claims}
        state = {**state, "verified": {**state["verified"],
                                       "claims": [c for c in state["verified"]["claims"] if c["id"] in audited]}}
        bridges = BridgePlan.model_validate(state.get("bridges") or {})
        ids = {"claims": [c.id for c in claims], "bridges": [b.id for b in bridges.bridges]}
        # the skeleton's support is a per-basis placeholder that fails the program check if copied as is (a fixed
        # "S1" was copied for every bridge, whatever its basis)
        out = json.dumps({"claims": [{"id": i, "verdict": "covered", "guide_quote": "…", "note": ""} for i in ids["claims"]],
                          "bridges": [{"id": b.id, "verdict": "ok", "correct": "yes", "support": SUPPORT_SKELETON[b.basis],
                                       "guide_quote": "…", "note": ""} for b in bridges.bridges],
                          "additions": [], "uncovered_source": [], "coherence": "pass", "coherence_note": ""}, ensure_ascii=False)
        msg, rec = await prompt_for(ctx, state, "body_audit", "reading_body_audit",
                                    with_optional("reading_body_audit", state,
                                                  ("quote_text", "symbol_evidence", "supplements", "claims", "bridges"),
                                                  ("bridge_refs",)),
                                    {"guide": d["guide"], "output_template": out}, ra.GUIDE_MAX_TOKENS)
        a, _ = await run_structured(ctx, [HumanMessage(content=msg)], BodyAudit, task="guide", node="body_audit",
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    check=gates.body_audit_check(claims, bridges),
                                    json_schema=gates.exact_ids_schema(BodyAudit, ids),
                                    meta={"step": "body_audit", "quote_text": t.text, "guide": d["guide"],
                                          "claims": state["verified"]["claims"], "bridge_ids": [b.id for b in bridges.bridges],
                                          "bridges": bridges.model_dump()["bridges"]},
                                    min_max_tokens=ra.GUIDE_MAX_TOKENS, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        program = [ra.Finding.model_validate(f) for f in state.get("findings") or [] if f["origin"] == "program"]
        fs = program + gates.coverage_findings(t.text, d["guide"], claims, bridges, a, bridge_refs(t))
        return {"findings": [f.model_dump() for f in fs],
                "audit": {**a.model_dump(), "program_covered": [x for x in all_ids if x not in audited]},
                "decision": judged(state, ctx.budget, fs, allow_verify=True)}

    async def verify(state, ctx):
        t = task(state)
        d = state["draft"]
        fs = [ra.Finding.model_validate(f) for f in state["findings"]]
        idx = ra.to_verify(fs)
        extra = {"chinese_text": "（這項核對只看主要正文）", "guide": d["guide"], "claims": ra.verify_items_text(fs, idx)}
        out_tokens = gates.verify_tokens(len(idx))
        # numbered sentences, so a bridge's source support can be cited by id; a session pinned to an older prompt
        # bundle still gets the plain text its template asks for (the program check is the same either way)
        source_key = "sentences" if "{{sentences}}" in prompts(state)["reading_verify"] else "quote_text"
        msg, rec = await prompt_for(ctx, state, "verify", "reading_verify",
                                    with_optional("reading_verify", state, (source_key,), ("bridge_refs",)), extra, out_tokens)
        v, _ = await run_structured(ctx, [HumanMessage(content=msg)], ra.VerifyResult, task="guide", node="verify",
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    meta={"step": "verify", "quote_text": t.text}, allow_fix=False,
                                    json_schema=gates.verify_schema(len(idx)),
                                    min_max_tokens=out_tokens, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        fs = ra.apply_verify(fs, idx, v, d, t.text,
                             {**bridge_refs(t), "statements": {c.id: c.statement for c in claims_of(state)}})
        return {"findings": [f.model_dump() for f in fs], "verified_for": state.get("body_revisions", 0),
                "decision": judged(state, ctx.budget, fs, allow_verify=False)}

    async def revise_body(state, ctx):
        t = task(state)
        ctx.budget.charge("revision")
        n_bridges = len((state.get("bridges") or {}).get("bridges") or [])
        out_tokens = gates.body_tokens(t.text, n_bridges)
        majors = [ra.Finding.model_validate(f) for f in state["findings"] if f["severity"] == "major"]
        extra = {"issues": ra.findings_text(majors), "draft_json": json.dumps(state["draft"], ensure_ascii=False)}
        msg, rec = await prompt_for(ctx, state, "revise_body", "reading_body_revise",
                                    with_optional("reading_body_revise", state,
                                                  ("body_source", "symbol_evidence", "claims", "bridges", "supplements"),
                                                  ("topic_context",)), extra,
                                    out_tokens)
        d, _ = await run_structured(ctx, [HumanMessage(content=msg)], BodyDraft, task="guide", prose_fields=("guide",), node="revise_body",
                                    json_schema=gates.body_schema(t.text, n_bridges),
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    purpose="revision", meta={"step": "revise_body", "quote_text": t.text,
                                                              "claims": state["verified"]["claims"],
                                                              "bridges": (state.get("bridges") or {}).get("bridges") or [],
                                                              "evidence_formulas": gates.evidence_formulas(state.get("symbol_evidence")),
                                                              "issues": [f.code for f in majors]},
                                    min_max_tokens=out_tokens, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        return {"draft": d.model_dump(), "body_revisions": state.get("body_revisions", 0) + 1,
                "history": [{"stage": "body", "draft": state["draft"], "findings": state["findings"]}]}

    def accept(state: GuideState) -> dict:
        if state.get("gate_rules") != gates.GATE_RULES_VERSION:
            # a run checked under other gate rules (e.g. resumed from an older checkpoint) is never published; the
            # runner restarts such runs before resuming, this is the last line of defence
            return {"status": "failed", "error": {"code": "gate_rules_changed",
                                                  "detail": "檢查規則在這段導讀產生期間更新了，需要依新規則重新檢查"}}
        t, v, d = state["task"], state["verified"], state["draft"]
        fs = [ra.Finding.model_validate(f) for f in state.get("findings") or []]
        bridges = BridgePlan.model_validate(state.get("bridges") or {})
        g2 = gates.gate2_result(claims_of(state), bridges, fs)
        minors = [f.model_dump() for f in fs if f.severity == "minor"]
        b = state["budget"]
        return {"status": "accepted", "gate2": g2.model_dump(), "artifact": {
            "chinese_text": v.get("chinese_text"), "guide": d["guide"], "key_points": d.get("key_points") or [],
            "check": d.get("check") or "", "limits": sorted(set((d.get("limits") or []) + (v.get("limits") or []) + (
                ["教學補充無法核對，這段只依原文導讀"] if state.get("bridges_dropped") else []))),
            "bridges_dropped": (state.get("bridges_dropped") or [{}])[0].get("bridges") or [],
            "supplement_notes": [x.content for x in bridges.bridges if x.boundary == "supplement"],
            "source_claims": v["claims"], "verified_source_key": v["key"], "gate1": v["gate1"], "gate2": g2.model_dump(),
            "coverage": (state.get("audit") or {}).get("claims"),
            "bridges": bridges.model_dump(),
            # what the draft says it used, less the bridges the checks could not find in the guide
            "bridges_used": [x for x in d.get("bridges_used") or []
                             if not any(f.code == "bridge_unconfirmed" and f.item == x for f in fs)],
            "unit_id": t["unit_id"], "source_refs": list(t["block_ids"]) + supplement_ids(state),
            "context_status": state.get("context_status"),
            "review_status": "passed_with_warnings" if minors else "passed", "warnings": minors,
            "unverified": gates.reader_notes(fs),
            "revised": state.get("body_revisions", 0) > 0 or state.get("source_revisions", 0) > 0,
            "prompt_version": t["prompt_bundle_hash"], "model_config_hash": t["model_config_hash"],
            "workflow_version": ra.READING_WORKFLOW_VERSION, "contract_version": CONTRACT_VERSION,
            "learning_hash": task(state).learning.hash, "gate_rules": state["gate_rules"],
            "calls": b["calls"], "attempt": t["attempt"], "agent": "langgraph"}}

    def hold(state: GuideState) -> dict:
        """Not published: the understanding / draft and the reasons stay in the state (and in a rejected artifact)."""
        if state.get("status") == "failed" and state.get("error"):
            return {}
        if state.get("verified") is None:
            failing = [c for c in (state.get("gate1") or {}).get("checks") or [] if c["status"] in ("fail", "unverified")]
            kinds = {c["kind"] for c in failing}
            code = ("notation_unverified" if kinds == {"notation"} else "context_missing" if kinds == {"context"}
                    else "source_unverified")
            return {"status": "failed", "error": {"code": code, "detail": "來源理解沒有通過檢查：" +
                                                  "；".join(c["detail"] for c in failing)[:400]}}
        majors = [f for f in state.get("findings") or [] if f["severity"] == "major"]
        return {"status": "failed", "error": {"code": "quality_check_failed",
                                              "detail": "導讀正文沒有通過檢查：" + "；".join(f["detail"] for f in majors)[:400]}}

    g = StateGraph(GuideState)
    g.add_node("prepare", prepare)
    for name, fn in (("symbols", symbols), ("understand", understand), ("context", context), ("source_audit", source_audit),
                     ("revise_source", revise_source), ("plan_bridges", plan_bridges), ("draft", draft),
                     ("body_audit", body_audit), ("verify", verify), ("revise_body", revise_body)):
        g.add_node(name, model_node(name, fn))
    def source_done(state: GuideState) -> dict:
        return {"status": "source_verified"}

    for name, fn in (("gate1", gate1), ("gate2", gate2), ("drop_bridges", drop_bridges), ("accept", accept), ("hold", hold),
                     ("source_done", source_done)):
        g.add_node(name, fn)

    def failed_or(route):
        return lambda s: "hold" if s.get("status") == "failed" else route(s)

    judge = {"pass": "accept", "revise": "revise_body", "verify": "verify", "fail": "hold", "drop_bridges": "drop_bridges"}
    g.add_edge(START, "prepare")
    after_source = lambda s: "source_done" if s.get("source_only") else "plan_bridges"   # noqa: E731
    g.add_conditional_edges("prepare", lambda s: after_source(s) if s.get("verified") else
                            "symbols" if needs_symbols(s) else "understand",
                            ["plan_bridges", "source_done", "symbols", "understand"])
    g.add_conditional_edges("symbols", failed_or(lambda s: "understand"), ["understand", "hold"])
    g.add_conditional_edges("understand", failed_or(lambda s: "context" if wants_model_context(s) else "gate1"),
                            ["context", "gate1", "hold"])
    g.add_conditional_edges("context", failed_or(lambda s: "understand" if s.get("understanding") is None else "gate1"),
                            ["understand", "gate1", "hold"])
    g.add_conditional_edges("gate1", lambda s: {"pass": after_source(s), "audit": "source_audit", "revise": "revise_source",
                                                "hold": "hold"}[s["decision"]],
                            ["plan_bridges", "source_done", "source_audit", "revise_source", "hold"])
    g.add_conditional_edges("source_audit", failed_or(lambda s: "gate1"), ["gate1", "hold"])
    g.add_conditional_edges("revise_source", failed_or(lambda s: "gate1"), ["gate1", "hold"])
    g.add_conditional_edges("plan_bridges", failed_or(lambda s: "draft"), ["draft", "hold"])
    g.add_conditional_edges("draft", failed_or(lambda s: "gate2"), ["gate2", "hold"])
    g.add_conditional_edges("gate2", lambda s: {"audit": "body_audit", "revise": "revise_body", "fail": "hold",
                                                "drop_bridges": "drop_bridges"}[s["decision"]],
                            ["body_audit", "revise_body", "drop_bridges", "hold"])
    g.add_conditional_edges("body_audit", failed_or(lambda s: judge[s["decision"]]),
                            ["accept", "revise_body", "verify", "drop_bridges", "hold"])
    g.add_conditional_edges("verify", failed_or(lambda s: judge.get(s["decision"], "hold") if s["decision"] != "verify" else "hold"),
                            ["accept", "revise_body", "drop_bridges", "hold"])
    g.add_edge("drop_bridges", "draft")
    g.add_conditional_edges("revise_body", failed_or(lambda s: "gate2"), ["gate2", "hold"])
    g.add_edge("source_done", END)
    g.add_edge("accept", END)
    g.add_edge("hold", END)
    return g.compile(checkpointer=checkpointer)
