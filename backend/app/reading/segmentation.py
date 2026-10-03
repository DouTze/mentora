"""Source units from source blocks (plan §4.2).

    blocks → program marks protected relations (glue) and forced boundaries
           → planning windows (owned blocks + read-only neighbours), never cut through a glued pair
           → model returns groups of block labels + a short reason (WindowPlan)
           → program validates coverage / order / contiguity / glue / heading rule; one repair; else rules
           → seams between windows: glue cannot cross a window cut; a connective-led seam may be checked by the
             model (≤ 2 calls, 0 when the rule decides)
           → units (+ parts), whose text the program rebuilds from the blocks.

The rules planner alone is a complete, valid plan (used when the model is off, unavailable for a window after its
budget, or keeps producing invalid plans); it satisfies every protected relation by construction."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage

from ..llm.runtime import CallBudget, stable_hash
from ..llm.tasks import TaskContext, TaskError, run_structured
from .schemas import PlannedUnit, ReadingPart, SegmentationPlan, SourceBlock, SourceUnit, WindowPlan
from pydantic import BaseModel

SEGMENTATION_VERSION = "units-v1"
MIN_UNIT_CHARS = 40
MAX_UNIT_CHARS = 700
WINDOW_CHARS = 5000
CONTEXT_BLOCKS = 2
CONTEXT_PREVIEW = 300
PART_CHARS = 5000
TOKEN_METHOD = "heuristic-cjk1.0-other0.3"
WHERE_RE = re.compile(r"^\s*(其中|式中|這裡的?|此處|where\b|here\b|with\b)", re.I)
CONNECTIVE_RE = re.compile(r"^\s*(因此|所以|於是|由此|然而|但是|不過|此外|另外|同理|換句話說|也就是說|therefore|thus|hence|however|so\b|consequently)", re.I)


class PlanInvalid(Exception):
    pass


def estimate_tokens(text: str) -> int:
    """Conservative without a tokenizer: one token per CJK char, ~0.3 per other char (BPE on Latin ≈ 3.5 chars)."""
    cjk = len(re.findall(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]", text))
    return int(cjk + (len(text) - cjk) * 0.3) + 1


# -- protected relations ------------------------------------------------------------

def glue_reason(a: SourceBlock, b: SourceBlock) -> str | None:
    """Why a and the following b must be in the same unit (None: they may be split)."""
    if b.kind == "heading":
        return None
    if a.kind == "heading":
        return "標題必須和其下內容同單元"
    if a.continues:
        return "句子跨頁／跨區塊延續"
    if a.kind in ("paragraph", "list_item") and a.text.rstrip().endswith(("：", ":")):
        return "引入句與其後內容"
    if b.kind == "formula" and a.kind in ("paragraph", "list_item"):
        return "公式與其引入"
    if a.kind == "formula" and WHERE_RE.match(b.text):
        return "公式與其 where 說明"
    if a.kind == "list_item" and b.kind == "list_item":
        return "同一清單"
    if a.kind == "figure":
        return "圖與說明文字"
    if a.parent_block_id and a.parent_block_id == b.parent_block_id and not ends_safely(a.text):
        return "同一段落"
    return None


def ends_safely(text: str) -> bool:
    t = text.rstrip().rstrip("」』）)】\"'”’")
    return bool(t) and t[-1] in "。！？!?.;；"


def glues(blocks: list[SourceBlock]) -> list[str | None]:
    """glues[i] = reason blocks[i] and blocks[i+1] must stay together, else None."""
    return [glue_reason(a, b) for a, b in zip(blocks, blocks[1:])]


def forced_break(blocks: list[SourceBlock], i: int) -> bool:
    """A unit must start at blocks[i]: a heading that follows non-heading content."""
    return i > 0 and blocks[i].kind == "heading" and blocks[i - 1].kind != "heading"


def body_chars(blocks: list[SourceBlock]) -> int:
    return sum(len(b.text) for b in blocks if b.kind != "heading")


# -- rules planner --------------------------------------------------------------------

def rules_groups(blocks: list[SourceBlock]) -> list[list[int]]:
    """One paragraph-level block per unit; glued blocks together; headings open a unit; a tiny block joins its
    neighbour when that stays under MAX_UNIT_CHARS (same granularity as the legacy candidate paragraphs)."""
    g = glues(blocks)
    groups: list[list[int]] = []
    for i, b in enumerate(blocks):
        if not groups or forced_break(blocks, i):
            groups.append([i])
            continue
        cur = groups[-1]
        if g[i - 1] or blocks[cur[-1]].kind == "heading":
            cur.append(i)
            continue
        cur_chars = body_chars([blocks[j] for j in cur])
        small = cur_chars < MIN_UNIT_CHARS or (len(b.text) < MIN_UNIT_CHARS and b.kind == "paragraph")
        if small and cur_chars + len(b.text) <= MAX_UNIT_CHARS and b.kind not in ("code", "table"):
            cur.append(i)
        else:
            groups.append([i])
    return groups


def validate_groups(blocks: list[SourceBlock], groups: list[list[int]], lo: int = 0, hi: int | None = None) -> list[str]:
    """Errors of a grouping of blocks[lo:hi] (empty list = valid)."""
    hi = len(blocks) if hi is None else hi
    errs: list[str] = []
    flat = [i for grp in groups for i in grp]
    if flat != list(range(lo, hi)):
        seen: set[int] = set()
        dup = [i for i in flat if i in seen or seen.add(i)]
        miss = sorted(set(range(lo, hi)) - set(flat))
        if dup:
            errs.append("重複使用的區塊：" + "、".join(f"B{i - lo + 1}" for i in dup))
        if miss:
            errs.append("沒有用到的區塊：" + "、".join(f"B{i - lo + 1}" for i in miss))
        if not dup and not miss:
            errs.append("區塊順序被打亂；每個單元必須依原文順序、連續")
        return errs
    g = glues(blocks)
    for grp in groups:
        for k, i in enumerate(grp):
            if k and blocks[i].kind == "heading" and blocks[grp[k - 1]].kind != "heading":
                errs.append(f"B{i - lo + 1} 是小標題，必須作為新單元的開頭")
        last = grp[-1]
        if last + 1 < hi and g[last]:
            errs.append(f"B{last - lo + 1} 與 B{last - lo + 2} 不可切開（{g[last]}）")
    return errs


# -- windows ----------------------------------------------------------------------

@dataclass
class Window:
    index: int
    lo: int
    hi: int                     # owned blocks[lo:hi]
    before: list[int] = field(default_factory=list)
    after: list[int] = field(default_factory=list)

    @property
    def window_id(self) -> str:
        return f"w{self.index + 1:02d}"


def plan_windows(blocks: list[SourceBlock], window_chars: int = WINDOW_CHARS) -> list[Window]:
    """Consecutive owned ranges of about window_chars; a cut never falls inside a glued pair (the window grows
    instead) and prefers a heading."""
    g = glues(blocks)
    out: list[Window] = []
    lo, size = 0, 0
    for i, b in enumerate(blocks):
        if i > lo and size + len(b.text) > window_chars and not g[i - 1]:
            out.append(Window(len(out), lo, i))
            lo, size = i, 0
        size += len(b.text)
    out.append(Window(len(out), lo, len(blocks)))
    for w in out:
        w.before = list(range(max(0, w.lo - CONTEXT_BLOCKS), w.lo))
        w.after = list(range(w.hi, min(len(blocks), w.hi + CONTEXT_BLOCKS)))
    return out


def window_listing(blocks: list[SourceBlock], w: Window) -> tuple[str, dict]:
    g = glues(blocks)
    kinds = {"heading": "小標題", "paragraph": "段落", "list_item": "清單項目", "formula": "公式", "code": "程式碼",
             "table": "表格", "figure": "圖（只有說明文字，圖片內容無法讀取）", "quote": "引文"}
    lines = []
    if w.before:
        lines.append("### 前文（唯讀，只供理解，不要輸出）")
        lines += [f"［前文］{blocks[i].text[-CONTEXT_PREVIEW:]}" for i in w.before]
        lines.append("")
    lines.append("### 要分配的區塊")
    for i in range(w.lo, w.hi):
        b = blocks[i]
        note = f"　⟶ 必須與下一個區塊同單元（{g[i]}）" if i + 1 < w.hi and g[i] else ""
        lines.append(f"【B{i - w.lo + 1}】（{kinds[b.kind]}）{note}\n{b.text}")
    if w.after:
        lines.append("")
        lines.append("### 後文（唯讀，只供理解，不要輸出）")
        lines += [f"［後文］{blocks[i].text[:CONTEXT_PREVIEW]}" for i in w.after]
    meta = {"blocks": [{"label": f"B{i - w.lo + 1}", "kind": blocks[i].kind, "chars": len(blocks[i].text),
                        "glue_next": bool(i + 1 < w.hi and g[i])} for i in range(w.lo, w.hi)]}
    return "\n\n".join(lines), meta


def labels_to_groups(plan: WindowPlan, w: Window) -> list[list[int]]:
    groups = []
    n = w.hi - w.lo
    for u in plan.units:
        grp = []
        for lab in u.blocks:
            m = re.fullmatch(r"\s*B?(\d+)\s*", str(lab))
            if not m or not 1 <= int(m.group(1)) <= n:
                raise PlanInvalid(f"不存在的區塊編號：{lab}（只能用 B1–B{n}）")
            grp.append(w.lo + int(m.group(1)) - 1)
        groups.append(grp)
    return groups


class SeamDecision(BaseModel):
    merge: bool
    reason: str = ""


# -- planner --------------------------------------------------------------------------

@dataclass
class PlannerDeps:
    """How to call the model for one window / seam; None → rules only."""
    make_ctx: Any = None            # (task: str, key: str) -> TaskContext with a fresh budget for that task
    prompts: dict[str, str] = field(default_factory=dict)   # segment_blocks, segment_seam (session snapshot)
    prompt_version: str = ""
    material_title: str = ""


async def plan_window(blocks: list[SourceBlock], w: Window, deps: PlannerDeps) -> tuple[list[list[int]], list[str], dict]:
    """(groups, reasons, info). Model plan validated by the program; one repair with the concrete errors; rules
    when it stays invalid. A connection failure propagates (the caller keeps the work retryable)."""
    rules = [[i for i in grp if w.lo <= i < w.hi] for grp in rules_groups(blocks[:w.hi])]
    rules = [grp for grp in rules if grp]
    info: dict[str, Any] = {"window_id": w.window_id, "owned": [blocks[i].block_id for i in range(w.lo, w.hi)],
                            "context": [blocks[i].block_id for i in w.before + w.after], "planner": "rules", "notes": []}
    if deps.make_ctx is None:
        return rules, ["規則切法"] * len(rules), info
    listing, meta = window_listing(blocks, w)
    prompt = deps.prompts["segment_blocks"].replace("{{material_title}}", deps.material_title) \
        .replace("{{count}}", str(w.hi - w.lo)).replace("{{blocks}}", listing)
    ctx: TaskContext = deps.make_ctx("segment_window", w.window_id)
    msgs = [HumanMessage(content=prompt)]
    holder: dict[str, Any] = {}

    def check(plan: WindowPlan) -> None:
        try:
            groups = labels_to_groups(plan, w)
        except PlanInvalid as e:
            from ..llm.tasks import OutputInvalid
            raise OutputInvalid(str(e)) from e
        errs = validate_groups(blocks, groups, w.lo, w.hi)
        if errs:
            from ..llm.tasks import OutputInvalid
            raise OutputInvalid("；".join(errs))
        holder["groups"] = groups

    try:
        plan, recs = await run_structured(ctx, msgs, WindowPlan, task="segment_window", node=f"segment.{w.window_id}",
                                          prompt_version=deps.prompt_version, meta={"step": "segment_blocks", **meta},
                                          source_ids=info["owned"], check=check, min_max_tokens=1024,
                                          min_context_tokens=16384)
    except TaskError as e:
        if e.kind == "unavailable":
            raise
        info["notes"].append(f"{w.window_id} 模型切段無效，改用規則切法：{e.detail[:160]}")
        info["calls"] = ctx.budget.calls
        return rules, ["規則切法（模型切段無效）"] * len(rules), info
    info["planner"] = "model"
    info["calls"] = ctx.budget.calls
    info["format_fixes"] = ctx.budget.format_fixes
    return holder["groups"], [u.reason or "" for u in plan.units], info


async def check_seam(blocks: list[SourceBlock], left: list[int], right: list[int], deps: PlannerDeps) -> tuple[bool, str, int]:
    """Should the last unit of one window and the first unit of the next be one unit? Glue cannot cross a window
    cut, so the rule only escalates when the right side opens with a connective (因此／然而…); merged size is
    capped by MAX_UNIT_CHARS × 2. Returns (merge, reason, model calls)."""
    first = next((blocks[i] for i in right if blocks[i].kind != "heading"), None)
    if blocks[right[0]].kind == "heading" or first is None or not CONNECTIVE_RE.match(first.text):
        return False, "規則：接縫處沒有承接語", 0
    if body_chars([blocks[i] for i in left + right]) > MAX_UNIT_CHARS * 2 or deps.make_ctx is None:
        return False, "規則：合併後過長或未啟用模型", 0
    ctx: TaskContext = deps.make_ctx("seam", f"{blocks[left[0]].block_id}|{blocks[right[0]].block_id}")
    prompt = deps.prompts["segment_seam"].replace("{{left}}", "\n\n".join(blocks[i].text for i in left)) \
        .replace("{{right}}", "\n\n".join(blocks[i].text for i in right))
    try:
        d, _ = await run_structured(ctx, [HumanMessage(content=prompt)], SeamDecision, task="seam", node="segment.seam",
                                    prompt_version=deps.prompt_version, meta={"step": "segment_seam"},
                                    source_ids=[blocks[i].block_id for i in left + right], min_max_tokens=256)
    except TaskError as e:
        if e.kind == "unavailable":
            raise
        return False, f"接縫檢查無效，維持切開：{e.detail[:80]}", ctx.budget.calls
    return d.merge, d.reason or ("模型判斷合併" if d.merge else "模型判斷切開"), ctx.budget.calls


async def plan_units(blocks: list[SourceBlock], deps: PlannerDeps, *, window_chars: int = WINDOW_CHARS,
                     done_windows: dict[str, dict] | None = None, on_window=None) -> SegmentationPlan:
    """Whole-range plan. `done_windows` (window_id → saved result) lets a restarted job reuse finished windows;
    `on_window(window_id, result)` is called after each window so the caller can persist it."""
    done_windows = done_windows or {}
    windows = plan_windows(blocks, window_chars)
    all_groups: list[list[int]] = []
    reasons: list[str] = []
    infos, warnings = [], []
    seam_at: list[int] = []
    for w in windows:
        saved = done_windows.get(w.window_id)   # keys: window ids and "seam:<first>|<last>"
        if saved and saved.get("owned") == [blocks[i].block_id for i in range(w.lo, w.hi)]:
            groups, rs, info = saved["groups"], saved["reasons"], saved["info"]
        else:
            groups, rs, info = await plan_window(blocks, w, deps)
            if on_window:
                on_window(w.window_id, {"owned": info["owned"], "groups": groups, "reasons": rs, "info": info})
        if all_groups:
            seam_at.append(len(all_groups))
        all_groups += groups
        reasons += rs
        infos.append(info)
        warnings += info["notes"]
    # seams, right to left so indices stay valid
    for k in reversed(seam_at):
        key = f"seam:{blocks[all_groups[k - 1][0]].block_id}|{blocks[all_groups[k][-1]].block_id}"
        if key in done_windows:
            merge, why = done_windows[key]["merge"], done_windows[key]["reason"]
        else:
            merge, why, _ = await check_seam(blocks, all_groups[k - 1], all_groups[k], deps)
            if on_window:
                on_window(key, {"merge": merge, "reason": why})
        for info in infos:
            info.setdefault("seams", [])
        infos[min(len(infos) - 1, seam_at.index(k) + 1)].setdefault("seams", []).append({"merge": merge, "reason": why})
        if merge:
            all_groups[k - 1:k + 1] = [all_groups[k - 1] + all_groups[k]]
            reasons[k - 1:k + 1] = [f"{reasons[k - 1]}；接縫合併：{why}"]
    errs = validate_groups(blocks, all_groups)
    if errs:   # cannot happen with validated windows; never ship an unchecked plan
        warnings.append("整體驗證失敗，改用規則切法：" + "；".join(errs))
        all_groups = rules_groups(blocks)
        reasons = ["規則切法"] * len(all_groups)
    planners = {i["planner"] for i in infos}
    units = [SourceUnit(unit_id=f"unit-{stable_hash([blocks[g[0]].block_id, blocks[g[-1]].block_id], 10)}",
                        ordinal=k, block_ids=[blocks[i].block_id for i in g],
                        title=None, boundary_reason=reasons[k] if k < len(reasons) else "",
                        context_refs=[], segmentation_version=SEGMENTATION_VERSION)
             for k, g in enumerate(all_groups)]
    return SegmentationPlan(units=units, warnings=warnings, planner=planners.pop() if len(planners) == 1 else "mixed",
                            windows=infos)


# -- units → legacy quote / part shapes ---------------------------------------------------

def _label(unit: str, blocks: list[SourceBlock]) -> str:
    body = [b for b in blocks if b.kind != "heading"] or blocks
    a, z = body[0].locator, body[-1].locator
    if unit == "page":
        if a.start != z.end:
            return f"第 {a.start}–{z.end} 頁"
        base = f"第 {a.start} 頁"
        if (a.paras or 1) <= 1:
            label = base
        else:
            label = base + (f" 第 {a.para} 段" if a.para == z.para else f" 第 {a.para}–{z.para} 段")
    else:
        label = f"第 {a.start} 行" if a.start == z.end else f"第 {a.start}–{z.end} 行"
    pieces = [b.piece for b in body if b.piece]
    if pieces and len({b.parent_block_id for b in body if b.piece}) == 1 and len(body) == len(pieces):
        label += f"（之 {pieces[0]}）" if len(pieces) == 1 else f"（之 {pieces[0]}–{pieces[-1]}）"
    return label


def excerpt(text: str, head: int = 24, tail: int = 16) -> str:
    from ..tutor.reading import _excerpt
    return _excerpt(text, head, tail)


def unit_text(blocks: list[SourceBlock]) -> str:
    """Body text rebuilt from the snapshot (headings go to the heading field, as in the legacy quotes)."""
    return "\n\n".join(b.text for b in blocks if b.kind != "heading")


def to_quotes(unit: str, plan: SegmentationPlan, by_id: dict[str, SourceBlock]) -> list[dict]:
    quotes = []
    pending_heading: str | None = None
    for u in plan.units:
        bs = [by_id[i] for i in u.block_ids]
        heads = [b.text for b in bs if b.kind == "heading"]
        text = unit_text(bs)
        if not text:   # a unit of headings only (range ends on a heading): show it on the next / last quote
            pending_heading = " › ".join(filter(None, [pending_heading, *heads]))
            continue
        heading = " › ".join(filter(None, [pending_heading, *heads])) or None
        pending_heading = None
        quotes.append({"id": f"quote-{len(quotes) + 1:02d}", "index": len(quotes), "label": _label(unit, bs),
                       "heading": heading, "excerpt": excerpt(text), "text": text, "chars": len(text), "part": 0,
                       "unit_id": u.unit_id, "block_ids": u.block_ids, "boundary_reason": u.boundary_reason,
                       "has_figure": any(b.kind == "figure" for b in bs)})
    if pending_heading and quotes:
        quotes[-1]["heading"] = " › ".join(filter(None, [quotes[-1]["heading"], pending_heading]))
    return quotes


def plan_parts(quotes: list[dict], part_chars: int = PART_CHARS) -> tuple[list[dict], list[ReadingPart]]:
    """Legacy part dicts (quote_from/quote_to/chars/label) + ReadingPart contracts with token estimates."""
    from ..tutor.reading import plan_parts as legacy_plan_parts
    parts = legacy_plan_parts(quotes, part_chars)
    contracts = []
    for p in parts:
        qs = quotes[p["quote_from"]:p["quote_to"] + 1]
        p["part_id"] = f"part-{p['index'] + 1:02d}"
        p["estimated_tokens"] = sum(estimate_tokens(q["text"]) for q in qs)
        p["estimation_method"] = TOKEN_METHOD
        contracts.append(ReadingPart(part_id=p["part_id"], index=p["index"], unit_ids=[q["unit_id"] for q in qs],
                                     estimated_tokens=p["estimated_tokens"], estimation_method=TOKEN_METHOD))
    return parts, contracts
