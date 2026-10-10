"""Guided-reading subgraph v3: a teacher's handout for one 區塊, reviewed and corrected in place.

```mermaid
flowchart TD
    P[prepare] -->|PDF symbols| SY[symbols: rendered regions read by the session's vision model] --> I
    P --> I[intent: what this 導讀段 itself teaches, from its text, its summary and the 導讀區塊 overview]
    I --> D[draft: 講義 from the text and what it teaches only; the overview never reaches it]
    D --> R{review: program checks + A points taught + B no conflict with the textbook}
    R -->|nothing blocks| OK[accept]
    R -->|blocking, fewer than 3 corrections| F[fix: edits to the flagged passages only] --> R
    R & F -->|3 corrections done / an edit changed nothing| S{settle}
    S -->|a contradiction is left| H[hold: not published]
    S -->|only points / program facts left| OK2[accept with notes]
```

- The textbook is the textbook; the handout makes clear what this 導讀段 sets out to teach. That is read first, from
  the text itself (the overview only places it): written with the overview in view, a small model taught the later
  導讀段' content ahead. The handout may reorder, explain, add examples; it may not contradict or run ahead.
- A reviewer's finding counts only with quotations that are really in the texts (gates); otherwise it is a note.
- A correction is never a new draft: the corrector names the passages it replaces or adds after, the program
  applies them where they are, and only the changed passages are reviewed for conflicts again (the points still
  missing are asked again). At most three corrections; an edit that changes nothing stops the loop.
- A PDF 區塊 whose symbols cannot be confirmed is still written; the handout carries a note.
- Every model call is its own node round; its state update is stored as a `step` artifact under the operation key
  and the round, so a re-run after a crash reuses the reply; the call count is rebuilt from the trace, so a restart
  never refills the budget."""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from ..llm.context import ContextItem, ContextOverflow, Packet
from ..llm.runtime import CallBudget, ModelRuntime, TraceSink
from ..llm.tasks import TaskContext, TaskError, run_structured
from ..reading import agent as ra
from ..reading import gates
from ..reading import pdf_evidence
from ..reading.contracts import (CONTRACT_VERSION, ConflictsReview, HandoutDraft, HandoutFix, PointsReview,
                                 SymbolTranscription, TeachingIntent)
from . import faults

MAX_FIXES = 3


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
    rounds: dict                 # model node → times it has run (part of its step-cache key)
    symbol_evidence: dict | None # R5: the 區塊's symbol evidence, confirmed by the symbols node when possible
    intent: dict | None          # TeachingIntent: what this 導讀段 teaches (None: its summary stands in)
    draft: dict | None           # HandoutDraft as first written
    guide: str                   # the handout as it stands (after the corrections so far)
    findings: list[dict]         # what the last review left open
    fixes: int                   # corrections applied
    changed: list[str]           # passages the last correction wrote (reviewed for conflicts again)
    stalled: bool                # the last correction changed nothing
    refix: list[str]             # edits of the last correction that could not be applied: the next one is told why
    fix_log: list[dict]
    decision: str | None
    status: str                  # running | accepted | failed
    error: dict | None
    artifact: dict | None
    history: Annotated[list, operator.add]


@dataclass
class ReadingDeps:
    runtime_for: Callable[[str], ModelRuntime]          # session id → runtime of its model snapshot
    prompts_for: Callable[[str], dict[str, str]]        # session id → prompt bundle texts (language fields filled)
    sink: TraceSink
    repo: Any = None                                    # Repository; None = no step cache (eval / unit tests)
    on_stage: Callable[[str, str | None, str], None] | None = None   # (session id, command id, stage) progress hook
    render_region: Callable | None = None               # (pdf_path, page, bbox) → PNG bytes; default: pdf_evidence
    evidence_dir: Any = None                            # where rendered regions are kept (Path); None = not kept


def initial_state(task: ra.ReadingTaskInput, op: str, command_id: str | None = None,
                  budget: dict | None = None) -> GuideState:
    """`budget`: what an earlier, discarded run of the same attempt already spent (a run restarted because the gate
    rules changed keeps its spending; the budget is never refilled)."""
    return {"task": task.model_dump(), "op": op, "command_id": command_id, "gate_rules": gates.GATE_RULES_VERSION,
            "budget": budget or CallBudget.for_task("guide").to_dict(), "budget_base": int((budget or {}).get("calls") or 0),
            "rounds": {}, "symbol_evidence": task.symbol_evidence, "intent": None, "draft": None, "guide": "", "findings": [],
            "fixes": 0, "changed": [], "stalled": False, "fix_log": [], "decision": None, "status": "running",
            "error": None, "artifact": None, "history": []}


def build_reading_graph(deps: ReadingDeps, checkpointer=None):
    def spent_calls(state) -> int:
        """Calls this attempt has made: what earlier, discarded runs spent (budget_base) plus every call recorded
        under this run's operation key — failed calls included, even those the checkpoint never saw."""
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
                    e.kind, "review_invalid" if name == "review" else "invalid_model_output")
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

    # -- prompt fields ------------------------------------------------------------------------
    def symbol_text(ev: dict | None) -> str:
        if not ev or ev.get("status") != "confirmed":
            return "（這一段沒有經過確認的符號取證；公式照課本的寫法）"
        tr = ev.get("transcription") or {"formulas": [], "symbols": []}
        lines = ["依原書版面的視覺取證（以此為準，不要依文字層或常識改寫）："]
        lines += [f"- 公式：${f}$" for f in tr["formulas"]]
        lines += [f"- {x['symbol']}：{x['latex']}（{x['style']}{'' if x['script'] == 'none' else '，' + x['script']}）"
                  for x in tr["symbols"]]
        return "\n".join(lines)

    def overview_of(t: ra.ReadingTaskInput) -> str:
        """The 導讀區塊's overview: where this 導讀段 sits. Not the other 導讀段' summaries — shown them, a small
        model taught their content here."""
        return ((t.topic or {}).get("summary") or "").strip() or "（這一段就是一個完整的導讀區塊，沒有總覽）"

    def points_of(state, t: ra.ReadingTaskInput) -> list[tuple[str, str]]:
        """What the handout must make clear (review A checks it): what this 導讀段 teaches, or its summary."""
        teaches = ((state.get("intent") or {}).get("teaches")) or []
        return [(f"P{i}", x.strip()) for i, x in enumerate(teaches, 1) if x.strip()] or gates.handout_points(t)

    def items_for(state, t: ra.ReadingTaskInput, keys: tuple[str, ...]) -> list[ContextItem]:
        """Required: the textbook passage, the points, the handout under review; optional (dropped first when the
        call does not fit): where the 區塊 sits, what the reader read and asked before."""
        f = ra.task_fields(t)
        make = {
            "quote_text": lambda: ContextItem(key="quote_text", text=f["quote_text"], source_ids=t.block_ids,
                                              version=t.source_hash),
            "points": lambda: ContextItem(key="points", text=gates.points_text(points_of(state, t))),
            "role": lambda: ContextItem(key="role", text=((state.get("intent") or {}).get("role") or "（無）").strip()),
            "teaches": lambda: ContextItem(key="teaches", text="\n".join(f"- {p}" for _, p in points_of(state, t))),
            "symbol_evidence": lambda: ContextItem(key="symbol_evidence", text=symbol_text(state.get("symbol_evidence")),
                                                   version=(state.get("symbol_evidence") or {}).get("version")),
            "topic_context": lambda: ContextItem(key="topic_context", text=f"主題總摘要：{overview_of(t)}", required=False,
                                                 priority=3),
            "prev_points": lambda: ContextItem(key="prev_points", text=f["prev_points"], required=False, priority=2),
            "qa_context": lambda: ContextItem(key="qa_context", text=f["qa_context"], required=False, priority=1),
        }
        return [make[k]() for k in keys]

    def fixed_fields(t: ra.ReadingTaskInput) -> dict[str, str]:
        f = ra.task_fields(t)
        return {k: f[k] for k in ("material_title", "range_label", "position", "quote_label", "heading", "unit_id")} | {
            "depth": "略讀（skim）" if t.learning.depth == "skim" else "細讀（close）",
            "goal": t.learning.goal or "（未指定）", "background": t.learning.background or "（讀者沒有提供）"}

    async def prompt_for(ctx: TaskContext, state, node: str, template: str, keys: tuple[str, ...], extra: dict[str, str],
                         max_tokens: int):
        t = task(state)
        limits = await ctx.runtime.limits(ra.GUIDE_CONTEXT_TOKENS)
        try:
            return Packet(node, prompts(state)[template], items_for(state, t, keys), {**fixed_fields(t), **extra}).build(
                ctx.runtime.max_tokens(max_tokens), limits)
        except ContextOverflow as e:
            raise TaskError("context_overflow", e.detail) from e

    # -- nodes ------------------------------------------------------------------------------------
    def prepare(state: GuideState) -> dict:
        if deps.repo is not None:
            # the run's input, so that scripts/gate_cases.py can replay it later under other review rules (its model
            # calls are in llm_calls under the same operation key); first write wins on a resume
            t = state["task"]
            deps.repo.put_artifact(f"{state['op']}:input", kind="guide_input", status="cached", payload=dict(state),
                                   session_id=t["session_id"], unit_id=t["unit_id"])
        return {}

    def needs_symbols(state) -> bool:
        t = task(state)
        ev = state.get("symbol_evidence")
        if t.source_kind != "pdf":
            return False
        return (ev or {}).get("status") in ("needs_visual", "unavailable") or (ev is None and gates.symbol_like(t.text))

    async def symbols(state, ctx):
        """Read the 區塊's rendered PDF regions with the session's own model, if it can take images. Evidence that
        cannot be confirmed does not stop the handout: it is written without it and says so."""
        t = task(state)
        ev = dict(state.get("symbol_evidence") or {"status": "unavailable", "regions": [],
                                                   "reason": "這一段有數學符號，但沒有原始 PDF 的版面證據，無法確認符號的字型"})
        caps = await ctx.runtime.backend.capabilities()
        if ev["status"] == "unavailable" or not caps.vision:
            reason = ev.get("reason") if ev["status"] == "unavailable" else \
                f"{ev.get('reason')}；目前的模型（{ctx.runtime.backend.name} {ctx.runtime.backend.model}）不支援影像輸入，無法看圖核對"
            return {"symbol_evidence": {**ev, "status": "unverified", "reason": reason}}
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
        letter_fonts = clues.get("letter_fonts") or {}
        extra = {"clues": "\n".join(f"- {k}：{v}" for k, v in clues.items() if v and k != "letter_fonts") or "（無）",
                 "letter_fonts": gates.letter_fonts_text(letter_fonts)}
        limits = await ctx.runtime.limits(ra.GUIDE_CONTEXT_TOKENS)
        msg, rec = Packet("symbols", prompts(state)["reading_symbols"], items_for(state, t, ("quote_text",)),
                          {**fixed_fields(t), **extra}).build(ctx.runtime.max_tokens(1024), limits)
        parts[0]["text"] = msg
        tr, _ = await run_structured(ctx, [HumanMessage(content=parts)], SymbolTranscription, task="guide", node="symbols",
                                     prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                     meta={"step": "symbols", "quote_text": t.text, "regions": ev["regions"]},
                                     json_schema=gates.symbols_schema(), min_max_tokens=1024,
                                     min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        tr, fixes, conflicts = gates.fix_by_fonts(tr, letter_fonts)
        problems = conflicts + gates.transcription_against_clues(tr, clues)
        if problems:
            reason = "PDF 符號的視覺判讀與字型線索不一致：" + "；".join(problems)
            return {"symbol_evidence": {**ev, "status": "unverified", "reason": reason, "transcription": tr.model_dump(),
                                        "font_fixes": fixes, "renders": renders}}
        summary = "；".join(f"{x.symbol}={x.latex}" for x in tr.symbols)[:200]
        return {"symbol_evidence": {**ev, "status": "confirmed", "transcription": tr.model_dump(), "renders": renders,
                                    "font_fixes": fixes, "summary": summary,
                                    "model": f"{ctx.runtime.backend.name}:{ctx.runtime.backend.model}"}}

    async def intent(state, ctx):
        """What this 導讀段 itself sets out to teach, read from its text with its summary and the 導讀區塊 overview
        in view. An answer that cannot be read leaves the summary to stand in; the handout is still written."""
        t = task(state)
        extra = {"summary": gates.points_text(gates.handout_points(t)), "overview": overview_of(t)}
        msg, rec = await prompt_for(ctx, state, "intent", "reading_intent", ("quote_text",), extra, 1024)
        try:
            ti, _ = await run_structured(ctx, [HumanMessage(content=msg)], TeachingIntent, task="guide", node="intent",
                                         prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                         json_schema=gates.intent_schema(t.text),
                                         meta={"step": "intent", "quote_text": t.text, "points": gates.handout_points(t),
                                               "language": t.language},
                                         min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        except TaskError as e:
            if e.kind in ("unavailable", "budget", "context_overflow"):
                raise
            return {"intent": None}
        return {"intent": ti.model_dump()}

    async def draft(state, ctx):
        t = task(state)
        out_tokens = gates.handout_tokens(t.text)
        tmpl = prompts(state)["reading_body"]   # a session pinned to an older bundle asks for points and position
        keys = tuple(k for k in ("quote_text", "role", "teaches", "points", "symbol_evidence", "topic_context",
                                 "prev_points", "qa_context") if "{{" + k + "}}" in tmpl)
        msg, rec = await prompt_for(ctx, state, "draft", "reading_body", keys, {}, out_tokens)
        d, _ = await run_structured(ctx, [HumanMessage(content=msg)], HandoutDraft, task="guide", node="draft",
                                    prose_fields=("guide",), json_schema=gates.handout_schema(t.text),
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    meta={"step": "draft", "quote_text": t.text, "points": points_of(state, t), "language": t.language,
                                          "evidence_formulas": gates.required_formulas(t, state.get("symbol_evidence"))},
                                    min_max_tokens=out_tokens, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        return {"draft": d.model_dump(), "guide": d.guide.strip()}

    def symbol_note(state) -> list[ra.Finding]:
        ev = state.get("symbol_evidence") or {}
        if ev.get("status") != "unverified":
            return []
        return [ra.Finding(code="notation_unverified", check="program", severity="minor", status="unconfirmed",
                           detail=ev.get("reason") or "PDF 的數學符號沒能確認字型")]

    async def review(state, ctx):
        """Program checks, then review A (the points still to check) and review B (the whole handout the first time,
        afterwards only the passages the last correction wrote). What a correction did not touch keeps its verdict."""
        t = task(state)
        guide = state["guide"]
        prior = [ra.Finding.model_validate(f) for f in state.get("findings") or []]
        recheck = int(state.get("fixes") or 0) > 0
        points = points_of(state, t)
        still = {f.item for f in prior if f.code == "point_missing"}
        pending = [p for p in points if p[0] in still] if recheck else points
        found = gates.handout_program(t, guide, state.get("symbol_evidence")) + symbol_note(state)
        if pending:
            msg, rec = await prompt_for(ctx, state, "review", "reading_review_points", ("quote_text", "points"),
                                        {"guide": guide, "points": gates.points_text(pending),
                                         "output_template": gates.points_template(pending)}, 1024)
            a, _ = await run_structured(ctx, [HumanMessage(content=msg)], PointsReview, task="guide", node="review_points",
                                        prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                        json_schema=gates.exact_ids_schema(PointsReview, {"points": [i for i, _ in pending]}),
                                        meta={"step": "review_points", "quote_text": t.text, "guide": guide,
                                              "points": pending},
                                        min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
            found += gates.points_findings(pending, a, guide)
        if recheck:   # earlier verdicts on what no correction touched still stand
            found += [f for f in prior if f.check == "points" and f.item not in {i for i, _ in pending}]
            # a passage the last correction wrote (even back as it was) is reviewed again below: its new verdict
            # replaces the old one; only conflicts in passages it did not write are carried
            rewritten = [ra.norm_text(p) for p in state.get("changed") or []]
            found += [f for f in prior if f.check == "conflicts" and gates._locate(guide, f.draft_excerpt)
                      and not any(ra.norm_text(f.draft_excerpt) in p for p in rewritten)]
        passages = guide if not recheck else "\n\n".join(state.get("changed") or [])
        if passages.strip():
            msg, rec = await prompt_for(ctx, state, "review", "reading_review_conflicts", ("quote_text", "symbol_evidence"),
                                        {"guide": passages}, 1024)
            b, _ = await run_structured(ctx, [HumanMessage(content=msg)], ConflictsReview, task="guide",
                                        node="review_conflicts", prompt_version=t.prompt_bundle_hash, packet=rec,
                                        source_ids=t.block_ids, json_schema=gates.conflicts_schema(),
                                        meta={"step": "review_conflicts", "quote_text": t.text, "guide": passages},
                                        min_max_tokens=1024, min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
            found += gates.conflict_findings(b, guide, t.text)
        blocking = [f for f in found if f.severity == "major" and f.status == "confirmed"]
        if not blocking:
            decision = "accept"
        elif int(state.get("fixes") or 0) < MAX_FIXES and not state.get("stalled") and can(ctx.budget, 3):
            decision = "fix"
        else:
            decision = "settle"
        return {"findings": [f.model_dump() for f in found], "decision": decision}

    async def fix(state, ctx):
        """Correct the blocking problems in place: edits to the named passages only, applied by the program."""
        t = task(state)
        ctx.budget.charge("revision")
        guide = state["guide"]
        blocking = [ra.Finding.model_validate(f) for f in state["findings"]
                    if f["severity"] == "major" and f["status"] == "confirmed"]
        problems = gates.problems_text(blocking)
        if state.get("refix"):   # the last correction's edits pointed at passages the handout does not have
            problems += ("\n\n上一次的修正沒有套用，因為 old 在講義中找不到：" + "；".join(state["refix"])
                         + "。old 要從上面「目前的講義」一字不改地複製，只複製要換掉的那一兩句。")
        msg, rec = await prompt_for(ctx, state, "fix", "reading_fix", ("quote_text", "symbol_evidence"),
                                    {"guide": guide, "problems": problems}, gates.handout_tokens(t.text))
        x, _ = await run_structured(ctx, [HumanMessage(content=msg)], HandoutFix, task="guide", node="fix",
                                    prompt_version=t.prompt_bundle_hash, packet=rec, source_ids=t.block_ids,
                                    purpose="revision", json_schema=gates.fix_schema(t.text, len(blocking)),
                                    meta={"step": "fix", "quote_text": t.text, "guide": guide, "language": t.language,
                                          "problems": [f.model_dump() for f in blocking]},
                                    min_max_tokens=gates.handout_tokens(t.text), min_context_tokens=ra.GUIDE_CONTEXT_TOKENS)
        new, applied, skipped = gates.apply_edits(guide, x.edits)
        fixes = int(state.get("fixes") or 0) + 1
        # no edit could be applied: another review would see the same handout, so the next correction is asked
        # again, told which passages were not found; with the corrections spent it settles. A passage written back
        # as it was is the corrector disputing the problem; it is reviewed again (the new verdict replaces the old one)
        retry = not applied and fixes < MAX_FIXES and can(ctx.budget, 3)
        stalled = not applied and not retry
        return {"guide": new, "fixes": fixes, "stalled": stalled, "refix": (skipped or ["沒有給出任何 edit"]) if retry else [],
                "changed": [a["passage"] for a in applied if a["passage"]],
                "fix_log": (state.get("fix_log") or []) + [{"problems": [f.detail[:160] for f in blocking],
                                                             "applied": applied, "skipped": skipped}],
                "history": [{"stage": "fix", "guide": guide, "findings": state["findings"]}]}

    def settle(state: GuideState) -> dict:
        """The corrections are spent: a contradiction left is never published; points or program facts left are
        published as notes with the handout."""
        left = [f for f in state.get("findings") or [] if f["severity"] == "major" and f["status"] == "confirmed"]
        conflicts = [f for f in left if f["check"] == "conflicts"]
        if conflicts:
            return {"decision": "hold", "status": "failed",
                    "error": {"code": "conflict_with_textbook",
                              "detail": "講義有和課本矛盾的地方，修正後仍未解決：" + "；".join(f["detail"] for f in conflicts)[:400]}}
        return {"decision": "accept"}

    def accept(state: GuideState) -> dict:
        if state.get("gate_rules") != gates.GATE_RULES_VERSION:
            # a run checked under other review rules (e.g. resumed from an older checkpoint) is never published; the
            # runner restarts such runs before resuming, this is the last line of defence
            return {"status": "failed", "error": {"code": "gate_rules_changed",
                                                  "detail": "檢查規則在這段導讀產生期間更新了，需要依新規則重新檢查"}}
        t, d = state["task"], state["draft"] or {}
        fs = [ra.Finding.model_validate(f) for f in state.get("findings") or []]
        notes = gates.reader_notes(fs)
        ev = state.get("symbol_evidence") or {}
        return {"status": "accepted", "artifact": {
            "guide": state["guide"], "key_points": d.get("key_points") or [], "check": d.get("check") or "",
            "limits": [], "points": [p for _, p in points_of(state, task(state))], "role": (state.get("intent") or {}).get("role", ""),
            "unverified": notes, "warnings": [f.model_dump() for f in fs if f.severity == "minor"],
            "open_findings": [f.model_dump() for f in fs if f.severity == "major"],
            "review_status": "passed_with_notes" if notes else "passed",
            "revised": int(state.get("fixes") or 0) > 0, "fixes": int(state.get("fixes") or 0),
            "fix_log": state.get("fix_log") or [], "symbols": ev.get("status"),
            "unit_id": t["unit_id"], "source_refs": list(t["block_ids"]),
            "prompt_version": t["prompt_bundle_hash"], "model_config_hash": t["model_config_hash"],
            "workflow_version": ra.READING_WORKFLOW_VERSION, "contract_version": CONTRACT_VERSION,
            "learning_hash": task(state).learning.hash, "gate_rules": state["gate_rules"],
            "calls": state["budget"]["calls"], "attempt": t["attempt"], "agent": "langgraph"}}

    def hold(state: GuideState) -> dict:
        """Not published: the handout and the reasons stay in the state (and in a rejected artifact)."""
        if state.get("status") == "failed" and state.get("error"):
            return {}
        return {"status": "failed", "error": {"code": "quality_check_failed", "detail": "講義沒有通過檢查"}}

    g = StateGraph(GuideState)
    g.add_node("prepare", prepare)
    for name, fn in (("symbols", symbols), ("intent", intent), ("draft", draft), ("review", review), ("fix", fix)):
        g.add_node(name, model_node(name, fn))
    for name, fn in (("settle", settle), ("accept", accept), ("hold", hold)):
        g.add_node(name, fn)

    def failed_or(route):
        return lambda s: "hold" if s.get("status") == "failed" else route(s)

    g.add_edge(START, "prepare")
    # a session pinned to a bundle without the intent prompt writes from the 導讀段 summary, as it did
    before_draft = lambda s: "intent" if "reading_intent" in prompts(s) else "draft"   # noqa: E731
    g.add_conditional_edges("prepare", lambda s: "symbols" if needs_symbols(s) else before_draft(s),
                            ["symbols", "intent", "draft"])
    g.add_conditional_edges("symbols", failed_or(before_draft), ["intent", "draft", "hold"])
    g.add_conditional_edges("intent", failed_or(lambda s: "draft"), ["draft", "hold"])
    g.add_conditional_edges("draft", failed_or(lambda s: "review"), ["review", "hold"])
    g.add_conditional_edges("review", failed_or(lambda s: s["decision"]), ["accept", "fix", "settle", "hold"])
    g.add_conditional_edges("fix", failed_or(lambda s: "fix" if s.get("refix") else "settle" if s.get("stalled") else "review"),
                            ["fix", "review", "settle", "hold"])
    g.add_conditional_edges("settle", lambda s: "hold" if s.get("status") == "failed" else "accept", ["accept", "hold"])
    g.add_edge("accept", END)
    g.add_edge("hold", END)
    return g.compile(checkpointer=checkpointer)


__all__ = ["ReadingDeps", "ReadingUnavailable", "build_reading_graph", "initial_state"]
