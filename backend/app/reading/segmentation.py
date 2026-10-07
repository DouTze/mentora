"""Source units from source blocks (plan §4.2; R4).

Four levels that are no longer mixed:
- SourceBlock: the smallest traceable piece of extracted text (source.py);
- SourceUnit: a complete reading / teaching unit (a whole argument, a formula with its introduction and `where`, a
  list, an example with what it illustrates, code with its explanation, a figure with its caption);
- ReadingPart: the reading product, at most one formal third-level section (markdown `#`–`###`) or one topic;
- model batch (planning window): what one model call sees — a computing boundary only, never a semantic one.

    blocks → program marks protected relations (glue) and forced boundaries
           → a buffer (BUF) of consecutive blocks up to WINDOW_CHARS / WINDOW_TOKENS (+ read-only neighbours),
             never cut through a glued pair
           → model returns groups of block labels + a short reason (WindowPlan)
           → program checks order; one repair; else rules for that buffer. Then it repairs single units instead of
             discarding the buffer: a left-out block joins the unit before it, a cut through a glued pair moves past
             the pair, a unit over the size limit gets a focused split request (then a program split at headings /
             paragraph starts when that fails too)
           → every unit but the last is final; the last one may continue past the buffer, so its blocks go back
             into the next buffer, which is refilled after them (the last buffer keeps all its units)
           → units (+ parts), whose text the program rebuilds from the blocks.

For PDFs the blocks are sentences of a rebuilt text stream (source.py): the model decides paragraph-level units by
meaning, and `para_group` (the paragraph the program rebuilt from line ends) is only a hint.

The rules planner alone is a complete, valid plan (used when the model is off, unavailable for a window after its
budget, or keeps producing invalid plans); it satisfies every protected relation by construction."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage

from ..llm.runtime import CallBudget, stable_hash
from ..llm.tasks import TaskContext, TaskError, run_structured
from .schemas import (Boundaries, MatterLabels, Outline, ParagraphTopic, PlannedUnit, ReadingPart, SegmentationPlan, SourceBlock, SourceUnit, TopicStarts,
                      TopicSummary, UnitChunk, WindowPlan)

SEGMENTATION_VERSION = "units-v10"   # v2: argument / example / enumeration relations, section-bounded parts; v3: BUF carry-over; v4: topic units with a strict size limit; v5: per-unit repair; v6: one-topic check; v7: topic starts, headings stay with their section; v8: topic check via paragraph summaries; v9: units from 區塊 outlines, every unit carries its 區塊; v10: 區塊 that need no guide (contents, summaries) are left out
MIN_UNIT_CHARS = 40
MAX_UNIT_CHARS = 700
WINDOW_CHARS = 5000
WINDOW_TOKENS = 3000        # BUF limit in estimated tokens: ~3000 Chinese chars; Latin text stops at WINDOW_CHARS first
CONTEXT_BLOCKS = 2
CONTEXT_PREVIEW = 300
PART_CHARS = 5000
ARGUMENT_MAX_CHARS = 1600   # a soft relation (argument, example, enumeration) merges units up to this size
UNIT_MAX_TOKENS = 1600      # strict upper bound of one unit (~1600 Chinese chars): more is too much for a beginner at once
TOPIC_CHECK_MIN_CHARS = 400 # a model unit this long with two or more paragraphs gets the one-topic check
TOPIC_MIN_PART_CHARS = 120  # a one-topic split that leaves a smaller part is not taken (too fine for a guide)
SECTION_LEVEL = 3           # markdown heading depth mapped to the book's formal third-level section (Ch n.m.k)
CONCLUSION_RE = re.compile(r"^\s*(因此|所以|於是|由此可知|由此可見|由此|綜上|總之|故|therefore|thus|hence|consequently|as a result)", re.I)
ENUM_NEXT_RE = re.compile(r"^\s*(第[二三四五六七八九十]個?|其次|再者|另一個前提|second(ly)?|third(ly)?|next)", re.I)
ENUM_FIRST_RE = re.compile(r"第一|首先|一方面|\bfirst(ly)?\b", re.I)
EXAMPLE_RE = re.compile(r"^\s*(例如|舉例來說|舉例而言|比方說|譬如|以.{1,12}為例|for example|for instance|e\.g\.)", re.I)
CODE_EXPLAIN_RE = re.compile(r"^\s*(這段程式|上面的程式|上述程式|這個程式|這段程式碼|上面這段|the code above|this code|in this code|here,? )", re.I)
TABLE_EXPLAIN_RE = re.compile(r"^\s*(上表|此表|這張表|表中|the table above|this table)", re.I)
TOKEN_METHOD = "heuristic-cjk1.0-other0.3"
WHERE_RE = re.compile(r"^\s*(其中|式中|這裡的?|此處|where\b|here\b|with\b)", re.I)


class PlanInvalid(Exception):
    pass


def estimate_tokens(text: str) -> int:
    """Conservative without a tokenizer: one token per CJK char, ~0.3 per other char (BPE on Latin ≈ 3.5 chars)."""
    cjk = len(re.findall(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]", text))
    return int(cjk + (len(text) - cjk) * 0.3) + 1


# -- protected relations ------------------------------------------------------------

def glue_reason(a: SourceBlock, b: SourceBlock, enum_open: bool = False) -> str | None:
    """Why a and the following b must be in the same unit (None: they may be split). Hard relations always hold;
    soft ones ("軟：…" — argument, example, enumeration, code / table explanation) hold while the merged unit stays
    under ARGUMENT_MAX_CHARS (see glue_plan)."""
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
        # a piece cut from a long paragraph: inside a sentence it must stay; at a sentence end the planner may start a
        # new topic there
        return "同一段落"
    if b.kind in ("paragraph", "list_item"):
        if CONCLUSION_RE.match(b.text) and a.kind in ("paragraph", "list_item", "formula"):
            return "軟：論證的結論承接前提"
        if ENUM_NEXT_RE.match(b.text) and (enum_open or ENUM_FIRST_RE.search(a.text) or ENUM_NEXT_RE.match(a.text)):
            return "軟：同一組列舉的前提或步驟"
        if EXAMPLE_RE.match(b.text) and a.kind in ("paragraph", "list_item", "formula"):
            return "軟：例子與它說明的內容"
        if a.kind == "code" and CODE_EXPLAIN_RE.match(b.text):
            return "軟：程式碼與其說明"
        if a.kind == "table" and TABLE_EXPLAIN_RE.match(b.text):
            return "軟：表格與其說明"
    return None


def glue_plan(blocks: list[SourceBlock]) -> tuple[list[str | None], list[dict]]:
    """(glues, relations). A soft relation that would grow its chain beyond ARGUMENT_MAX_CHARS is not enforced as
    one unit; it is kept as a relation (the units stay in the same Part and the later one gets the earlier one as
    program context)."""
    raw, enum_open = [], False
    for a, b in zip(blocks, blocks[1:]):
        enum_open = (enum_open or bool(ENUM_FIRST_RE.search(a.text))) and a.kind != "heading"
        raw.append(glue_reason(a, b, enum_open))
    out: list[str | None] = []
    relations: list[dict] = []
    # rest[i]: blocks[i] plus the rest of its paragraph, which usually goes with it
    rest = [len(b.text) for b in blocks]
    for i in range(len(blocks) - 2, -1, -1):
        if same_para(blocks, i + 1):
            rest[i] += rest[i + 1]
    chain = len(blocks[0].text) if blocks else 0
    for i, g in enumerate(raw):
        nxt = len(blocks[i + 1].text)
        if g and g.startswith("軟：") and chain + rest[i + 1] > ARGUMENT_MAX_CHARS:
            relations.append({"from": blocks[i].block_id, "to": blocks[i + 1].block_id, "reason": g[2:],
                              "note": f"合併後超過 {ARGUMENT_MAX_CHARS} 字，分成相鄰單元並保持在同一部分"})
            g = None
        out.append(g)
        # the rest of a paragraph usually goes with it, so it counts towards the size a soft relation would create
        chain = chain + nxt if g or same_para(blocks, i + 1) else nxt
    return out, relations


def ends_safely(text: str) -> bool:
    t = text.rstrip().rstrip("」』）)】\"'”’")
    return bool(t) and t[-1] in "。！？!?.;；"


def glues(blocks: list[SourceBlock]) -> list[str | None]:
    """glues[i] = reason blocks[i] and blocks[i+1] must stay together, else None."""
    return glue_plan(blocks)[0]


def forced_break(blocks: list[SourceBlock], i: int) -> bool:
    """The rules planner starts a unit at blocks[i]: a heading that follows non-heading content. For the model this is
    guidance (a heading usually opens a new small topic), not a checked rule."""
    return i > 0 and blocks[i].kind == "heading" and blocks[i - 1].kind != "heading"


def para_key(b: SourceBlock) -> str | None:
    return b.meta.get("para_group") or b.parent_block_id


def same_para(blocks: list[SourceBlock], i: int) -> bool:
    """blocks[i] continues the paragraph of blocks[i - 1]: one the PDF extractor rebuilt, or a long paragraph cut into
    pieces (a hint, not a protected relation)."""
    k = para_key(blocks[i])
    return bool(k) and i > 0 and para_key(blocks[i - 1]) == k


def unit_tokens(blocks: list[SourceBlock]) -> int:
    return sum(estimate_tokens(b.text) for b in blocks)


def body_chars(blocks: list[SourceBlock]) -> int:
    return sum(len(b.text) for b in blocks if b.kind != "heading")


# -- rules planner --------------------------------------------------------------------

def rules_groups(blocks: list[SourceBlock]) -> list[list[int]]:
    """One paragraph-level block per unit; glued blocks together; headings open a unit; sentences of one rebuilt PDF
    paragraph stay together up to ARGUMENT_MAX_CHARS; a tiny block joins its neighbour when that stays under
    MAX_UNIT_CHARS (same granularity as the legacy candidate paragraphs)."""
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
        if same_para(blocks, i) and cur_chars + len(b.text) <= ARGUMENT_MAX_CHARS \
                and unit_tokens([blocks[j] for j in cur] + [b]) <= UNIT_MAX_TOKENS:
            cur.append(i)
            continue
        small = cur_chars < MIN_UNIT_CHARS or (len(b.text) < MIN_UNIT_CHARS and b.kind == "paragraph")
        if small and cur_chars + len(b.text) <= MAX_UNIT_CHARS and b.kind not in ("code", "table"):
            cur.append(i)
        else:
            groups.append([i])
    return groups


def validate_groups(blocks: list[SourceBlock], groups: list[list[int]], lo: int = 0, hi: int | None = None, *,
                    size_limit: bool = True, skipped: set[int] | None = None) -> list[str]:
    """Errors of a grouping of blocks[lo:hi] (empty list = valid). Only what the program must guarantee is checked:
    coverage and order, protected relations (glue) and — for the buffer planner, whose guide is one model call per
    unit — the strict size limit; where a topic begins is the model's call. Units from 區塊 outlines have no size
    limit (their guide is written one 區塊 at a time)."""
    hi = len(blocks) if hi is None else hi
    errs: list[str] = []
    flat = [i for grp in groups for i in grp]
    skipped = skipped or set()
    if flat != [i for i in range(lo, hi) if i not in skipped]:
        seen: set[int] = set()
        dup = [i for i in flat if i in seen or seen.add(i)]
        miss = sorted(set(range(lo, hi)) - set(flat) - skipped)
        if dup:
            errs.append("重複使用的區塊：" + "、".join(f"B{i - lo + 1}" for i in dup))
        if miss:
            errs.append("沒有用到的區塊：" + "、".join(f"B{i - lo + 1}" for i in miss))
        if not dup and not miss:
            errs.append("區塊順序被打亂；每個單元必須依原文順序、連續")
        return errs
    g = glues(blocks)
    for grp in groups:
        if size_limit and unit_tokens([blocks[i] for i in grp]) > UNIT_MAX_TOKENS and any(not g[i] for i in grp[:-1]):
            errs.append(f"B{grp[0] - lo + 1}–B{grp[-1] - lo + 1} 太長（上限約 {UNIT_MAX_TOKENS} 字），"
                        "請在主題轉換的句子之間拆開")
        last = grp[-1]
        if last + 1 < hi and g[last]:
            errs.append(f"B{last - lo + 1} 與 B{last - lo + 2} 不可切開（{g[last]}）")
    return errs


def oversize(blocks: list[SourceBlock], grp: list[int], g: list[str | None] | None = None) -> bool:
    """Over the strict size limit and splittable (some boundary inside is not glued)."""
    g = glues(blocks) if g is None else g
    return unit_tokens([blocks[i] for i in grp]) > UNIT_MAX_TOKENS and any(not g[i] for i in grp[:-1])


def normalize_groups(blocks: list[SourceBlock], groups: list[list[int]], lo: int, hi: int) -> tuple[list[list[int]], list[str]]:
    """Repairs a model grouping of blocks[lo:hi] that keeps the original order: a left-out block joins the unit before
    it (the first unit for leading ones); a boundary right after a heading moves back before it (a heading belongs to
    what follows it: moving forward would pull the heading and the first sentence of its section into the unit
    before), and a boundary inside another glued pair moves forward past the pair. Raises PlanInvalid when blocks
    repeat or are out of order (that needs the model). Returns (groups, notes)."""
    flat = [i for grp in groups for i in grp]
    if any(b <= a for a, b in zip(flat, flat[1:])):
        errs = validate_groups(blocks, groups, lo, hi)
        raise PlanInvalid("；".join(errs) or "區塊順序被打亂；每個單元必須依原文順序、連續")
    notes: list[str] = []
    groups = [list(grp) for grp in groups if grp]
    miss = sorted(set(range(lo, hi)) - set(flat))
    if miss:
        notes.append("模型漏掉的區塊由程式補回前一個單元：" + "、".join(f"B{i - lo + 1}" for i in miss))
        for i in miss:
            k = max([k for k, grp in enumerate(groups) if grp[0] < i], default=0)
            groups[k] = sorted(groups[k] + [i])
    g = glues(blocks)
    out: list[list[int]] = []
    for grp in groups:
        back = 0
        while out and grp and blocks[out[-1][-1]].kind == "heading" and g[out[-1][-1]]:
            grp.insert(0, out[-1].pop())
            back += 1
            if not out[-1]:
                out.pop()
        if back:
            notes.append(f"B{grp[0] - lo + 1} 是小標題，要和下面的內容同單元，程式把邊界往前移")
        moved = 0
        while out and grp and g[out[-1][-1]]:
            out[-1].append(grp.pop(0))
            moved += 1
        if moved:
            notes.append(f"B{out[-1][-1] - moved - lo + 1} 與下一個區塊不可切開，程式把邊界往後移")
        if grp:
            out.append(grp)
    return out, notes


def split_by_size(blocks: list[SourceBlock], grp: list[int]) -> list[list[int]]:
    """Program split of an oversize unit: cut before the block that would pass the limit, at the best earlier
    boundary that is not glued (a heading first, then a paragraph start, then any sentence end; the latest of those)."""
    g = glues(blocks)
    out: list[list[int]] = []
    cur: list[int] = []
    for i in grp:
        seq = cur + [i]
        if cur and unit_tokens([blocks[j] for j in seq]) > UNIT_MAX_TOKENS:
            cuts = [k for k in range(1, len(seq)) if not g[seq[k] - 1]]
            if cuts:
                k = max(cuts, key=lambda k: (2 if blocks[seq[k]].kind == "heading" else
                                             0 if same_para(blocks, seq[k]) else 1, k))
                out.append(seq[:k])
                cur = seq[k:]
                continue
        cur = seq
    if cur:
        out.append(cur)
    return out


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


def buffer_end(blocks: list[SourceBlock], lo: int, g: list[str | None], window_chars: int = WINDOW_CHARS,
               window_tokens: int = WINDOW_TOKENS) -> int:
    """End (exclusive) of the buffer that starts at blocks[lo]: about window_chars characters / window_tokens
    estimated tokens, at least one block; a cut never falls inside a glued pair (the buffer grows instead)."""
    size = tokens = 0
    i = lo
    while i < len(blocks):
        t = blocks[i].text
        if i > lo and (size + len(t) > window_chars or tokens + estimate_tokens(t) > window_tokens) and not g[i - 1]:
            break
        size, tokens = size + len(t), tokens + estimate_tokens(t)
        i += 1
    return i


def make_window(blocks: list[SourceBlock], index: int, lo: int, hi: int) -> Window:
    return Window(index, lo, hi, before=list(range(max(0, lo - CONTEXT_BLOCKS), lo)),
                  after=list(range(hi, min(len(blocks), hi + CONTEXT_BLOCKS))))


def window_listing(blocks: list[SourceBlock], w: Window) -> tuple[str, dict]:
    g = glues(blocks)
    kinds = {"heading": "小標題", "paragraph": "段落", "list_item": "清單項目", "formula": "公式", "code": "程式碼",
             "table": "表格", "figure": "圖（只有說明文字，圖片內容無法讀取）", "quote": "引文"}

    def kind(i: int) -> str:
        if not blocks[i].meta.get("para_group"):
            return kinds[blocks[i].kind]
        return "句子，接續上一句的段落" if i > w.lo and same_para(blocks, i) else "句子，程式猜測的段落開頭"

    lines = []
    if w.before:
        lines.append("### 前文（唯讀，只供理解，不要輸出）")
        lines += [f"［前文］{blocks[i].text[-CONTEXT_PREVIEW:]}" for i in w.before]
        lines.append("")
    lines.append("### 要分配的區塊")
    for i in range(w.lo, w.hi):
        b = blocks[i]
        note = f"　⟶ 必須與下一個區塊同單元（{g[i]}）" if i + 1 < w.hi and g[i] else ""
        lines.append(f"【B{i - w.lo + 1}】（{kind(i)}）{note}\n{b.text}")
    if w.after:
        lines.append("")
        lines.append("### 後文（唯讀，只供理解，不要輸出）")
        lines += [f"［後文］{blocks[i].text[:CONTEXT_PREVIEW]}" for i in w.after]
    meta = {"blocks": [{"label": f"B{i - w.lo + 1}", "kind": blocks[i].kind, "chars": len(blocks[i].text),
                        "glue_next": bool(i + 1 < w.hi and g[i]), "same_para": i > w.lo and same_para(blocks, i)}
                       for i in range(w.lo, w.hi)]}
    return "\n\n".join(lines), meta


def labels_to_groups(plan: WindowPlan, w: Window) -> list[list[int]]:
    groups = []
    n = w.hi - w.lo
    for u in plan.units:
        grp = []
        for lab in u.blocks:   # "B3" or a range "B3-B7"
            m = re.fullmatch(r"\s*B?(\d+)\s*(?:[-–~～至到]\s*B?(\d+)\s*)?", str(lab))
            a, z = (int(m.group(1)), int(m.group(2) or m.group(1))) if m else (0, 0)
            if not m or not 1 <= a <= z <= n:
                raise PlanInvalid(f"不存在的區塊編號：{lab}（只能用 B1–B{n}）")
            grp += [w.lo + k - 1 for k in range(a, z + 1)]
        groups.append(grp)
    return groups


# -- planner --------------------------------------------------------------------------

@dataclass
class PlannerDeps:
    """How to call the model for one buffer; None → rules only."""
    make_ctx: Any = None            # (task: str, key: str) -> TaskContext with a fresh budget for that task
    prompts: dict[str, str] = field(default_factory=dict)   # segment_blocks (session snapshot)
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
            holder["groups"], holder["notes"] = normalize_groups(blocks, labels_to_groups(plan, w), w.lo, w.hi)
        except PlanInvalid as e:
            from ..llm.tasks import OutputInvalid
            raise OutputInvalid(str(e)) from e

    try:
        plan, recs = await run_structured(ctx, msgs, WindowPlan, task="segment_window", node=f"segment.{w.window_id}",
                                          prompt_version=deps.prompt_version, meta={"step": "segment_blocks", **meta},
                                          source_ids=info["owned"], check=check, min_max_tokens=2048,
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
    info["notes"] += [f"{w.window_id} {n}" for n in holder["notes"]]
    reasons = reasons_for(plan, holder["groups"], labels_to_groups(plan, w))
    # an oversize unit gets the focused split (which already looks for topic turns); the other units this buffer
    # emits get the one-topic check, concurrently (the backend pool limits real parallelism). The last unit may be
    # carried into the next buffer and planned again there, so it is not checked here.
    n = len(holder["groups"])
    final = n - 1 if w.hi < len(blocks) and n > 1 else n

    async def repair(k: int, grp: list[int], why: str) -> tuple[list[list[int]], list[str], str | None]:
        if oversize(blocks, grp):
            return await split_unit(blocks, grp, why, w, k, deps)
        if k < final and needs_topic_check(blocks, grp):
            return await check_topic(blocks, grp, why, w, k, deps)
        return [grp], [why], None

    groups, out_reasons = [], []
    for sub_groups, sub_reasons, note in await asyncio.gather(*(repair(k, grp, why) for k, (grp, why)
                                                                in enumerate(zip(holder["groups"], reasons)))):
        if note:
            info["notes"].append(f"{w.window_id} {note}")
        groups += sub_groups
        out_reasons += sub_reasons
    return groups, out_reasons, info


def reasons_for(plan: WindowPlan, groups: list[list[int]], raw: list[list[int]]) -> list[str]:
    """The model's reason for each repaired group: the reason of the planned unit its first block came from."""
    owner = {i: (u.reason or "") for u, grp in zip(plan.units, raw) for i in grp}
    return [owner.get(grp[0], "") for grp in groups]


async def split_unit(blocks: list[SourceBlock], grp: list[int], reason: str, w: Window, k: int,
                     deps: PlannerDeps) -> tuple[list[list[int]], list[str], str]:
    """One oversize unit: a focused request that lists only its blocks and asks where the topic turns; units of that
    answer still over the limit, or a failed answer, are split by the program. (groups, reasons, note)"""
    label = f"B{grp[0] - w.lo + 1}–B{grp[-1] - w.lo + 1}"
    template = deps.prompts.get("segment_split")
    if template is None:   # a session whose prompt snapshot predates the focused split
        parts = split_by_size(blocks, grp)
        return parts, ["（程式依長度拆開）"] * len(parts), f"{label} 太長，程式依長度拆開"
    sw = make_window(blocks, w.index, grp[0], grp[-1] + 1)
    listing, meta = window_listing(blocks, sw)
    prompt = template.replace("{{material_title}}", deps.material_title).replace("{{count}}", str(len(grp))) \
        .replace("{{reason}}", reason or "（未說明）").replace("{{limit}}", str(UNIT_MAX_TOKENS)).replace("{{blocks}}", listing)
    key = f"{w.window_id}-u{k + 1:02d}"
    ctx: TaskContext = deps.make_ctx("segment_split", key)
    holder: dict[str, Any] = {}

    def check(plan: WindowPlan) -> None:
        from ..llm.tasks import OutputInvalid
        try:
            groups, _ = normalize_groups(blocks, labels_to_groups(plan, sw), sw.lo, sw.hi)
        except PlanInvalid as e:
            raise OutputInvalid(str(e)) from e
        if len(groups) < 2:
            raise OutputInvalid(f"這 {len(grp)} 個區塊超過上限，必須拆成兩個以上的單元")
        holder["groups"] = groups

    try:
        plan, _ = await run_structured(ctx, [HumanMessage(content=prompt)], WindowPlan, task="segment_split",
                                       node=f"segment.{key}", prompt_version=deps.prompt_version,
                                       meta={"step": "segment_split", **meta},
                                       source_ids=[blocks[i].block_id for i in grp], check=check,
                                       min_max_tokens=2048, min_context_tokens=16384)
    except TaskError as e:
        if e.kind == "unavailable":
            raise
        parts = split_by_size(blocks, grp)
        return parts, ["（程式依長度拆開）"] * len(parts), f"{label} 太長，聚焦拆分無效，程式依長度拆開：{e.detail[:120]}"
    reasons = reasons_for(plan, holder["groups"], labels_to_groups(plan, sw))
    groups, out_reasons = [], []
    for sub_grp, why in zip(holder["groups"], reasons):
        parts = split_by_size(blocks, sub_grp) if oversize(blocks, sub_grp) else [sub_grp]
        groups += parts
        out_reasons += [why] * len(parts)
    return groups, out_reasons, f"{label} 太長，依模型指出的主題轉折拆成 {len(groups)} 個單元"


def paragraph_count(blocks: list[SourceBlock], grp: list[int]) -> int:
    """Paragraphs of a unit's body: a block opens one unless it continues the paragraph of the block before it."""
    return sum(1 for k, i in enumerate(grp) if blocks[i].kind != "heading" and not (k and same_para(blocks, i)))


def needs_topic_check(blocks: list[SourceBlock], grp: list[int]) -> bool:
    return body_chars([blocks[i] for i in grp]) >= TOPIC_CHECK_MIN_CHARS and paragraph_count(blocks, grp) >= 2


TOPIC_PARA_PREVIEW = 400    # chars of one paragraph shown to the topic summary


def topic_paragraphs(blocks: list[SourceBlock], grp: list[int]) -> list[list[int]]:
    """The unit as paragraphs for the topic check: one opens at a heading or a paragraph start, never between glued
    blocks (a heading stays with its first paragraph). Only how the text is shown; where topics change is the
    model's call."""
    g = glues(blocks)
    out = [[grp[0]]]
    for i in grp[1:]:
        if (blocks[i].kind == "heading" or not same_para(blocks, i)) and not g[i - 1]:
            out.append([i])
        else:
            out[-1].append(i)
    return out


async def check_topic(blocks: list[SourceBlock], grp: list[int], reason: str, w: Window, k: int,
                      deps: PlannerDeps) -> tuple[list[list[int]], list[str], str | None]:
    """Is one planned unit one topic? Asked directly, a small model was unstable (qwen3-vl:4b on a textbook: the
    overview and its first item split once in three, a single stage split twice in three). So the question comes in
    two steps that it answers well: first the topic and aspect of every paragraph (an overview of several items is
    "…總覽", an item's own details, limits and examples keep the item as topic), then, from those lines only, the
    paragraphs where a new topic begins. The decision prompt's example is the empty list, so copying it cuts nothing.
    A split is taken only when every part can carry a guide on its own (TOPIC_MIN_PART_CHARS); an invalid answer
    keeps the unit. (groups, reasons, note)"""
    summary_t, decide_t = deps.prompts.get("segment_topic_summary"), deps.prompts.get("segment_topic")
    if summary_t is None or decide_t is None:   # a session whose prompt snapshot predates this check
        return [grp], [reason], None
    label = f"B{grp[0] - w.lo + 1}–B{grp[-1] - w.lo + 1}"
    paras = topic_paragraphs(blocks, grp)
    n = len(paras)
    if n < 2:
        return [grp], [reason], None
    previews = ["\n".join([blocks[i].text for i in p if blocks[i].kind == "heading"]
                          + [unit_text([blocks[i] for i in p if blocks[i].kind != "heading"])]).strip() for p in paras]
    listing = "\n\n".join(f"【P{j}】\n{t[:TOPIC_PARA_PREVIEW]}{'…' if len(t) > TOPIC_PARA_PREVIEW else ''}"
                          for j, t in enumerate(previews, 1))
    key = f"{w.window_id}-t{k + 1:02d}"
    ctx: TaskContext = deps.make_ctx("segment_topic", key)
    source_ids = [blocks[i].block_id for i in grp]
    summary_schema = TopicSummary.model_json_schema()
    summary_schema["properties"]["paragraphs"] = {**summary_schema["properties"]["paragraphs"], "minItems": n, "maxItems": n}
    item = summary_schema["$defs"]["ParagraphTopic"]["properties"]
    item["topic"] = {**item["topic"], "maxLength": 40}
    item["aspect"] = {**item["aspect"], "maxLength": 40}
    starts_schema = TopicStarts.model_json_schema()
    starts_schema["properties"]["new_topic_at"] = {"type": "array", "items": {"type": "integer", "enum": list(range(1, n + 1))},
                                                   "maxItems": n}

    def check_summary(ans: TopicSummary) -> None:
        from ..llm.tasks import OutputInvalid
        if len(ans.paragraphs) != n:
            raise OutputInvalid(f"paragraphs 必須剛好 {n} 個（每段一個），收到 {len(ans.paragraphs)} 個")

    def check_starts(ans: TopicStarts) -> None:
        from ..llm.tasks import OutputInvalid
        bad = [x for x in ans.new_topic_at if not 1 <= x <= n]
        if bad:
            raise OutputInvalid(f"new_topic_at 只能用 1–{n} 的段落號碼（收到 {bad}）")

    try:
        summ, _ = await run_structured(
            ctx, [HumanMessage(content=summary_t.replace("{{material_title}}", deps.material_title)
                               .replace("{{count}}", str(n)).replace("{{paragraphs}}", listing))],
            TopicSummary, task="segment_topic", node=f"segment.{key}.summary", prompt_version=deps.prompt_version,
            meta={"step": "segment_topic_summary", "paragraphs": n}, source_ids=source_ids, check=check_summary,
            json_schema=summary_schema, min_max_tokens=1024, min_context_tokens=16384)
        lines = "\n".join(f"P{j}：大主題「{x.topic.strip()}」｜{x.aspect.strip()}" for j, x in enumerate(summ.paragraphs, 1))
        starts, _ = await run_structured(
            ctx, [HumanMessage(content=decide_t.replace("{{material_title}}", deps.material_title)
                               .replace("{{count}}", str(n)).replace("{{summaries}}", lines))],
            TopicStarts, task="segment_topic", node=f"segment.{key}", prompt_version=deps.prompt_version,
            meta={"step": "segment_topic", "paragraphs": n}, source_ids=source_ids, check=check_starts,
            json_schema=starts_schema, min_max_tokens=512, min_context_tokens=16384)
    except TaskError as e:
        if e.kind == "unavailable":
            raise
        return [grp], [reason], f"{label} 單題檢查無效，維持一個單元：{e.detail[:120]}"
    cuts = sorted({paras[x - 1][0] for x in starts.new_topic_at if x > 1})   # P1 already opens the unit
    if not cuts:
        return [grp], [reason], None
    bounds = [grp[0]] + cuts + [grp[-1] + 1]
    groups, _ = normalize_groups(blocks, [list(range(a, b)) for a, b in zip(bounds, bounds[1:])], grp[0], grp[-1] + 1)
    if len(groups) < 2:
        return [grp], [reason], None
    if min(body_chars([blocks[i] for i in g]) for g in groups) < TOPIC_MIN_PART_CHARS:
        return [grp], [reason], f"{label} 單題檢查想拆成 {len(groups)} 個單元，但有一部分太短，維持一個單元"
    topic_of = {i: summ.paragraphs[j].topic.strip() for j, p in enumerate(paras) for i in p}
    reasons = [topic_of.get(g[0]) or topic_of.get(next((i for i in g if i in topic_of), g[0]), reason) for g in groups]
    return groups, reasons, f"{label} 單題檢查：裡面有 {len(groups)} 個主題（{'／'.join(reasons)}），依模型指出的主題轉折拆開"


# -- 區塊 and outline planning (units-v9) -------------------------------------------------------
# The unit (主題) is decided from short outlines of paragraph-sized 區塊, never from thousands of characters of
# source at once; each guide is later written one 區塊 at a time (docs/reading-block-guide-plan.md).

CHUNK_MAX_CHARS = 600     # a 區塊 longer than this is cut between sentences (one model call writes one 區塊's guide)
CHUNK_MIN_CHARS = 60      # a shorter 區塊 (a lead-in line, a one-line paragraph) joins its neighbour
OUTLINE_BATCH = 12        # 區塊 per segment_outline call
OUTLINE_PREVIEW = 500     # chars of one 區塊 shown to segment_outline
TOPIC_BATCH = 30          # outline lines per segment_topic call
BOUNDARY_BATCH = 8        # paragraphs per segment_boundaries call: with 18, qwen3-vl:4b copied the example for every pair


def make_chunks(blocks: list[SourceBlock], lo: int = 0, hi: int | None = None) -> list[list[int]]:
    """Group blocks[lo:hi] into 區塊: one paragraph each (the extractor's paragraph marks); a heading opens one and
    stays with its first paragraph; glued blocks are never separated; a paragraph longer than CHUNK_MAX_CHARS is
    cut between sentences; a 區塊 shorter than CHUNK_MIN_CHARS joins the next one (the previous one at the end or
    before a heading)."""
    hi = len(blocks) if hi is None else hi
    g = glues(blocks)
    raw: list[list[int]] = []
    for i in range(lo, hi):
        if not raw:
            raw.append([i])
            continue
        cur = raw[-1]
        if g[i - 1] or all(blocks[j].kind == "heading" for j in cur):
            cur.append(i)
            continue
        new_para = blocks[i].kind == "heading" or not same_para(blocks, i)
        if new_para or body_chars([blocks[j] for j in cur]) + len(blocks[i].text) > CHUNK_MAX_CHARS:
            raw.append([i])
        else:
            cur.append(i)
    out: list[list[int]] = []
    carry: list[int] = []
    for k, c in enumerate(raw):
        c = carry + c
        carry = []
        small = body_chars([blocks[i] for i in c]) < CHUNK_MIN_CHARS
        nxt = raw[k + 1] if k + 1 < len(raw) else None
        if small and nxt is not None and blocks[nxt[0]].kind != "heading":
            carry = c
        elif small and out and (nxt is None or blocks[nxt[0]].kind == "heading") and blocks[c[0]].kind != "heading":
            out[-1] += c
        else:
            out.append(c)
    if carry:
        if out:
            out[-1] += carry
        else:
            out.append(carry)
    return out


def chunk_preview(blocks: list[SourceBlock], c: list[int]) -> str:
    heads = [blocks[i].text for i in c if blocks[i].kind == "heading"]
    text = "\n".join(heads + [unit_text([blocks[i] for i in c if blocks[i].kind != "heading"])]).strip()
    return text[:OUTLINE_PREVIEW] + ("…" if len(text) > OUTLINE_PREVIEW else "")


def fallback_outline(blocks: list[SourceBlock], c: list[int]) -> ParagraphTopic:
    """When the model gave no usable outline: no topic (so no topic change is read into it), the opening words."""
    return ParagraphTopic(topic="（未判斷）", aspect="", summary=chunk_preview(blocks, c)[:60])


async def outline_chunks(blocks: list[SourceBlock], chunks: list[list[int]], deps: PlannerDeps, done: dict,
                         on_window, notes: list[str]) -> list[ParagraphTopic]:
    out: list[ParagraphTopic] = []
    for b, start in enumerate(range(0, len(chunks), OUTLINE_BATCH), 1):
        batch = chunks[start:start + OUTLINE_BATCH]
        key = f"o{b:02d}"
        owned = [blocks[i].block_id for c in batch for i in c]
        saved = done.get(key)
        if saved and saved.get("owned") == owned:
            out += [ParagraphTopic.model_validate(x) for x in saved["outline"]]
            continue
        n = len(batch)
        listing = "\n\n".join(f"【P{j}】\n{chunk_preview(blocks, c)}" for j, c in enumerate(batch, 1))
        prompt = deps.prompts["segment_outline"].replace("{{material_title}}", deps.material_title) \
            .replace("{{count}}", str(n)).replace("{{paragraphs}}", listing)
        schema = Outline.model_json_schema()
        schema["properties"]["paragraphs"] = {**schema["properties"]["paragraphs"], "minItems": n, "maxItems": n}
        item = schema["$defs"]["ParagraphTopic"]["properties"]
        item["topic"], item["aspect"], item["summary"] = ({**item[k], "maxLength": m} for k, m in
                                                          (("topic", 40), ("aspect", 40), ("summary", 80)))

        def check(ans: Outline, n=n) -> None:
            from ..llm.tasks import OutputInvalid
            if len(ans.paragraphs) != n:
                raise OutputInvalid(f"paragraphs 必須剛好 {n} 個（每段一個），收到 {len(ans.paragraphs)} 個")

        try:
            ans, _ = await run_structured(deps.make_ctx("segment_outline", key), [HumanMessage(content=prompt)], Outline,
                                          task="segment_outline", node=f"segment.{key}", prompt_version=deps.prompt_version,
                                          meta={"step": "segment_outline", "paragraphs": n,
                                                "headings": [next((blocks[i].text for i in c if blocks[i].kind == "heading"), "")
                                                             for c in batch]}, source_ids=owned, check=check,
                                          json_schema=schema, min_max_tokens=2048, min_context_tokens=16384)
            got = list(ans.paragraphs)
        except TaskError as e:
            if e.kind == "unavailable":
                raise
            notes.append(f"{key} 區塊摘要無效，以原文開頭代替：{e.detail[:120]}")
            got = [fallback_outline(blocks, c) for c in batch]
        if on_window:
            on_window(key, {"owned": owned, "outline": [x.model_dump() for x in got]})
        out += got
    return out


async def _boundaries(lines: list[ParagraphTopic], deps: PlannerDeps, key: str, owned: list[str],
                      notes: list[str]) -> list[int]:
    """Where a new unit begins, judged pair by pair from the paragraphs' content summaries (segment_boundaries).
    Asked where topics change from the outlines' topic labels, a small model cut wherever the labels differed, and
    they were written finely (every paragraph its own topic); asked to name each paragraph's unit, it named each
    paragraph. Comparing two neighbours at a time, with the whole stretch in view, it answered the same way every
    time. Returns 1-based paragraph numbers, like the older decision."""
    n = len(lines)
    said = [x.summary.strip() or x.topic.strip() for x in lines]
    prompt = deps.prompts["segment_boundaries"].replace("{{material_title}}", deps.material_title) \
        .replace("{{count}}", str(n)).replace("{{pair_count}}", str(n - 1)) \
        .replace("{{summaries}}", "\n".join(f"P{j}：{s}" for j, s in enumerate(said, 1))) \
        .replace("{{pairs}}", "\n".join(f"B{j}：P{j}「{said[j - 1]}」 → P{j + 1}「{said[j]}」" for j in range(1, n)))
    schema = Boundaries.model_json_schema()
    schema["properties"]["boundaries"] = {**schema["properties"]["boundaries"], "minItems": n - 1, "maxItems": n - 1}

    def check(ans: Boundaries) -> None:
        from ..llm.tasks import OutputInvalid
        if len(ans.boundaries) != n - 1:
            raise OutputInvalid(f"boundaries 必須剛好 {n - 1} 個（每對相鄰段落一個），收到 {len(ans.boundaries)} 個")

    try:
        ans, _ = await run_structured(deps.make_ctx("segment_boundaries", key), [HumanMessage(content=prompt)],
                                      Boundaries, task="segment_boundaries", node=f"segment.{key}",
                                      prompt_version=deps.prompt_version,
                                      meta={"step": "segment_boundaries", "paragraphs": n,
                                            "topics": [x.topic for x in lines]},
                                      source_ids=owned, check=check, json_schema=schema, min_max_tokens=512,
                                      min_context_tokens=16384)
        return [j + 2 for j, x in enumerate(ans.boundaries) if x == "new"]
    except TaskError as e:
        if e.kind == "unavailable":
            raise
        notes.append(f"{key} 主題判斷無效，改在大主題寫法改變處切開：{e.detail[:120]}")
        return [j for j in range(2, n + 1) if _topic_key(lines[j - 1].topic) != _topic_key(lines[j - 2].topic)
                and "未判斷" not in lines[j - 1].topic]


def _topic_key(t: str) -> str:
    return re.sub(r"[\s「」『』（）()：:，,。]", "", t or "")


async def topic_starts(outline: list[ParagraphTopic], deps: PlannerDeps, done: dict, on_window,
                       notes: list[str], ids: list[list[str]]) -> list[int]:
    """Indices of the 區塊 where a new unit begins (0 always). The lines go in batches; the last topic of a batch
    may continue, so the next batch starts at its first 區塊 (or, when a batch saw no change, at its last 區塊, so
    a change right after it can still be named). A 區塊 that opens a batch is never cut there."""
    n = len(outline)
    starts = {0}
    pos, b = 0, 0
    batch = BOUNDARY_BATCH if "segment_boundaries" in deps.prompts else TOPIC_BATCH
    while pos < n:
        b += 1
        hi = min(n, pos + batch)
        lines = outline[pos:hi]
        key = f"t{b:02d}"
        owned = [x for c in ids[pos:hi] for x in c]
        saved = done.get(key)
        if saved and saved.get("owned") == owned:
            local = saved["starts"]
        else:
            local = await _decide(lines, deps, key, owned, notes)
            if on_window:
                on_window(key, {"owned": owned, "starts": local})
        found = sorted(pos + x - 1 for x in local if 1 < x <= len(lines))
        starts |= set(found)
        if hi >= n:
            break
        nxt = found[-1] if found else hi - 1
        pos = max(nxt, pos + 1)
    return sorted(starts)


async def _decide(lines: list[ParagraphTopic], deps: PlannerDeps, key: str, owned: list[str], notes: list[str]) -> list[int]:
    n = len(lines)
    if n < 2:
        return []
    if "segment_boundaries" in deps.prompts:
        return await _boundaries(lines, deps, key, owned, notes)
    text = "\n".join(f"P{j}：大主題「{x.topic.strip()}」｜{x.aspect.strip()}" for j, x in enumerate(lines, 1))
    prompt = deps.prompts["segment_topic"].replace("{{material_title}}", deps.material_title) \
        .replace("{{count}}", str(n)).replace("{{summaries}}", text)
    schema = TopicStarts.model_json_schema()
    schema["properties"]["new_topic_at"] = {"type": "array", "items": {"type": "integer", "enum": list(range(1, n + 1))},
                                            "maxItems": n}

    def check(ans: TopicStarts) -> None:
        from ..llm.tasks import OutputInvalid
        bad = [x for x in ans.new_topic_at if not 1 <= x <= n]
        if bad:
            raise OutputInvalid(f"new_topic_at 只能用 1–{n} 的段落號碼（收到 {bad}）")

    try:
        ans, _ = await run_structured(deps.make_ctx("segment_topic", key), [HumanMessage(content=prompt)], TopicStarts,
                                      task="segment_topic", node=f"segment.{key}", prompt_version=deps.prompt_version,
                                      meta={"step": "segment_topic", "paragraphs": n, "topics": [x.topic for x in lines]},
                                      source_ids=owned, check=check, json_schema=schema, min_max_tokens=512,
                                      min_context_tokens=16384)
        return sorted(set(ans.new_topic_at))
    except TaskError as e:
        if e.kind == "unavailable":
            raise
        notes.append(f"{key} 主題判斷無效，改在大主題寫法改變處切開：{e.detail[:120]}")
        return [j for j in range(2, n + 1) if _topic_key(lines[j - 1].topic) != _topic_key(lines[j - 2].topic)
                and "未判斷" not in lines[j - 1].topic]


ROLE_NAMES = {"toc": "目錄", "summary": "摘要／重點回顧", "meta": "非學科內容"}
# a table-of-contents entry: dot leaders (or a wide gap) then a page number, also chapter-paged ("1-1", "3-12")
TOC_ENTRY_RE = re.compile(r"(?:[.．·・…]{4,}|…{2,}| {3,}|\t)\s*\d{1,4}(?:-\d{1,4})?(?!\d)")
# an entry without leaders: a numbered title ending in its page number ("3.1 The Agent–Environment Interface 48")
TOC_NUMBERED_RE = re.compile(r"^\s*(?:第.{1,6}[章節]|\d+(?:\.\d+)+)\s+\S.{0,80}?\s\d{1,4}(?:-\d{1,4})?\s*$")
TOC_TITLE_RE = re.compile(r"^\W*(?:目錄|目次|contents|table of contents)\W*$", re.I)
TOC_MIN_ENTRIES = 3        # entries in a row before it is a table of contents
TOC_ENTRY_CHARS = 120      # an entry block is entries, not prose that happens to end in a number
def toc_entries(text: str) -> int:
    lines = [x for x in (text or "").splitlines() if x.strip()]
    return len(TOC_ENTRY_RE.findall(text or "")) + sum(
        1 for x in lines if TOC_NUMBERED_RE.match(x) and not TOC_ENTRY_RE.search(x))


def toc_blocks(blocks: list[SourceBlock]) -> list[list[int]]:
    """Runs of source blocks that make a table of contents, found by the program before 區塊 are formed (a PDF puts
    each entry in a block of its own, glues one to the next paragraph, or several entries into one line): blocks
    that are mostly entries, at least TOC_MIN_ENTRIES of them in a row, with the 「目錄」 title just before."""
    runs: list[list[int]] = []
    run, n = [], 0

    def close():
        if n >= TOC_MIN_ENTRIES:
            k = run[0] - 1
            while k >= 0 and TOC_TITLE_RE.match(blocks[k].text or ""):
                run.insert(0, k)
                k -= 1
            runs.append(list(run))

    for i, b in enumerate(blocks):
        e = 0 if b.kind in ("code", "formula", "table") else toc_entries(b.text)
        if e and len(b.text) <= e * TOC_ENTRY_CHARS:
            run.append(i)
            n += e
        else:
            close()
            run, n = [], 0
    close()
    return runs


MATTER_BATCH = 40     # outline lines per segment_matter call: enough of the range to see what is lesson and what is not
REPEAT_MIN = 0.35     # share of a 區塊's wording already in the range before it: a summary repeats, new teaching does not


def repeated_share(blocks: list[SourceBlock], c: list[int], earlier: list[int]) -> float:
    """How much of 區塊 `c`'s wording (character bigrams) is already in the earlier blocks."""
    def grams(t: str) -> set[str]:
        t = re.sub(r"[^\w]", "", t or "")
        return {t[i:i + 2] for i in range(len(t) - 1)}
    own = grams(" ".join(blocks[i].text for i in c if blocks[i].kind != "heading"))
    if not own:
        return 0.0
    return len(own & grams(" ".join(blocks[i].text for i in earlier))) / len(own)


async def matter_marks(blocks: list[SourceBlock], chunks: list[list[int]], outline: list[ParagraphTopic],
                       deps: PlannerDeps, done: dict, on_window, notes: list[str]) -> dict[int, str]:
    """The 區塊 that are not lessons of the material's subject, judged on the outlines of a whole stretch at once
    (segment_matter): about the material, course or exam itself (meta), a summary of what was already taught
    (summary), contents (toc). Judged one paragraph at a time a small model calls nearly everything content; side
    by side, a chapter of exam rules reads as such next to the lessons after it. The program then keeps only what
    it can check for any book: a summary must repeat the text before it (a chapter's opening lesson does not), a
    table of contents must have the shape of one; code, formulas and tables are never left out."""
    if "segment_matter" not in deps.prompts or not chunks:
        return {}
    n = len(chunks)
    # two passes over batches cut at different places: what a paragraph is for should not depend on its neighbours
    # in one batch (a chapter's opening was called meta in one batch, lesson in another). Left out only where both
    # passes agree.
    first = await _matter_pass(blocks, chunks, outline, deps, done, on_window, notes, list(range(0, n, MATTER_BATCH)), "m")
    # the second pass's batches start half a batch later; a range shorter than one batch is cut in two halves, or
    # both passes would ask the very same question
    shifted = [0] + list(range(MATTER_BATCH // 2, n, MATTER_BATCH)) if n > MATTER_BATCH else [0, n // 2]
    second = await _matter_pass(blocks, chunks, outline, deps, done, on_window, notes, sorted(set(shifted)), "n")
    return {k: kind for k, kind in first.items() if k in second}


async def _matter_pass(blocks: list[SourceBlock], chunks: list[list[int]], outline: list[ParagraphTopic],
                       deps: PlannerDeps, done: dict, on_window, notes: list[str], starts: list[int],
                       tag: str) -> dict[int, str]:
    """One labelling pass, batches beginning at `starts`; returns the 區塊 the program accepts as needing no guide."""
    out: dict[int, str] = {}
    bounds = starts + [len(chunks)]
    for b, (start, end) in enumerate(zip(bounds, bounds[1:]), 1):
        idx = list(range(start, end))
        if not idx:
            continue
        key = f"{tag}{b:02d}"
        owned = [blocks[i].block_id for k in idx for i in chunks[k]]
        saved = done.get(key)
        if saved and saved.get("owned") == owned:
            marks = saved["marks"]
        else:
            lines = "\n".join(f"P{j}：大主題「{outline[k].topic.strip()}」｜{outline[k].summary.strip()}"
                              for j, k in enumerate(idx, 1))
            prompt = deps.prompts["segment_matter"].replace("{{material_title}}", deps.material_title) \
                .replace("{{count}}", str(len(idx))).replace("{{summaries}}", lines)
            # one label per paragraph, so many of them: asked only for the exceptions, the model copied the empty
            # example and listed none
            schema = MatterLabels.model_json_schema()
            schema["properties"]["labels"] = {**schema["properties"]["labels"], "minItems": len(idx), "maxItems": len(idx)}

            def check(ans: MatterLabels, n=len(idx)) -> None:
                from ..llm.tasks import OutputInvalid
                if len(ans.labels) != n:
                    raise OutputInvalid(f"labels 必須剛好 {n} 個（每段一個），收到 {len(ans.labels)} 個")

            try:
                ans, _ = await run_structured(deps.make_ctx("segment_matter", key), [HumanMessage(content=prompt)],
                                              MatterLabels, task="segment_matter", node=f"segment.{key}",
                                              prompt_version=deps.prompt_version,
                                              meta={"step": "segment_matter", "paragraphs": len(idx)},
                                              source_ids=owned, check=check, json_schema=schema, min_max_tokens=1024,
                                              min_context_tokens=16384)
                marks = [{"p": j, "kind": x} for j, x in enumerate(ans.labels, 1) if x != "lesson"]
            except TaskError as e:
                if e.kind == "unavailable":
                    raise
                notes.append(f"{key} 不需導讀內容的判斷無效，這一批全部保留：{e.detail[:120]}")
                marks = []
            if on_window:
                on_window(key, {"owned": owned, "marks": marks})
        for m in marks:
            k = idx[m["p"] - 1]
            c = chunks[k]
            if any(blocks[i].kind in ("code", "formula", "table") for i in c):
                continue
            if m["kind"] == "summary":
                earlier = [i for j in range(k) if j not in out for i in chunks[j]]
                if repeated_share(blocks, c, earlier) < REPEAT_MIN:
                    continue   # says something the range has not: it is teaching, whatever it is called
            if m["kind"] == "toc" and not toc_entries("\n".join(blocks[i].text for i in c)):
                continue
            out[k] = m["kind"]
    return out


def skipped_view(unit: str, plan: SegmentationPlan, by_id: dict[str, SourceBlock]) -> list[dict]:
    """What the reader is told was left out: where it is, what it was judged to be, and its text."""
    out = []
    for x in plan.skipped:
        bs = [by_id[i] for i in x["block_ids"] if i in by_id]
        if bs:
            out.append({"role": x["role"], "label": _label(unit, bs), "topic": x.get("topic", ""),
                        "text": "\n\n".join(b.text for b in bs)})
    return out


async def plan_by_outline(blocks: list[SourceBlock], deps: PlannerDeps, done_windows: dict, on_window) -> SegmentationPlan:
    notes: list[str] = []
    tocs = toc_blocks(blocks)
    in_toc = {i for r in tocs for i in r}
    if len(in_toc) == len(blocks):   # nothing but contents: keep it (an empty range would be worse)
        tocs, in_toc = [], set()
    chunks: list[list[int]] = []
    carry: list[int] = []
    for c in make_chunks(blocks):
        c = carry + [i for i in c if i not in in_toc]
        carry = []
        if c and all(blocks[i].kind == "heading" for i in c):   # headings left alone by the contents: next 區塊
            carry = c
        elif c:
            chunks.append(c)
    if carry:
        chunks.append(carry) if not chunks else chunks[-1].extend(carry)
    ids = [[blocks[i].block_id for i in c] for c in chunks]
    outline = await outline_chunks(blocks, chunks, deps, done_windows, on_window, notes)
    skip = await matter_marks(blocks, chunks, outline, deps, done_windows, on_window, notes)
    if len(skip) == len(chunks):   # a range that would leave nothing to read: keep it all
        notes.append("所有區塊都被判斷為不需要導讀，改為全部保留")
        skip = {}
    kept = [k for k in range(len(chunks)) if k not in skip]
    local = await topic_starts([outline[k] for k in kept], deps, done_windows, on_window, notes, [ids[k] for k in kept])
    # the units are where the topics change, nothing else: no size rule merges or splits them
    bounds = local + [len(kept)]
    units_k = [[kept[j] for j in range(a, b)] for a, b in zip(bounds, bounds[1:]) if b > a]
    counts: dict[str, int] = {}
    for role in ["toc"] * len(tocs) + list(skip.values()):
        counts[ROLE_NAMES[role]] = counts.get(ROLE_NAMES[role], 0) + 1
    if counts:
        notes.append("略過不需要導讀的內容：" + "、".join(f"{k} {n} 處" for k, n in counts.items()))
    groups = [[i for k in u for i in chunks[k]] for u in units_k]
    errs = validate_groups(blocks, groups, size_limit=False, skipped=in_toc | {i for k in skip for i in chunks[k]})
    if errs:   # chunks never cut a glued pair; never ship an unchecked plan
        raise PlanInvalid("；".join(errs))
    units = []
    for k, u in enumerate(units_k):
        g = groups[k]
        units.append(SourceUnit(
            unit_id=f"unit-{stable_hash([blocks[g[0]].block_id, blocks[g[-1]].block_id], 10)}", ordinal=k,
            block_ids=[blocks[i].block_id for i in g], boundary_reason=outline[u[0]].topic.strip(),
            segmentation_version=SEGMENTATION_VERSION,
            chunks=[UnitChunk(block_ids=ids[c], topic=outline[c].topic.strip(), aspect=outline[c].aspect.strip(),
                              summary=outline[c].summary.strip()) for c in u]))
    return SegmentationPlan(units=units, warnings=notes, planner="model", relations=glue_plan(blocks)[1],
                            windows=[{"window_id": "outline", "chunks": len(chunks), "notes": notes, "planner": "model"}],
                            skipped=sorted([{"block_ids": [blocks[i].block_id for i in r], "role": "toc", "topic": "目錄",
                                             "at": r[0]} for r in tocs]
                                           + [{"block_ids": ids[k], "role": role, "topic": outline[k].topic.strip(),
                                               "at": chunks[k][0]} for k, role in skip.items()],
                                           key=lambda x: x["at"]))


async def plan_units(blocks: list[SourceBlock], deps: PlannerDeps, *, window_chars: int = WINDOW_CHARS,
                     window_tokens: int = WINDOW_TOKENS, done_windows: dict[str, dict] | None = None,
                     on_window=None) -> SegmentationPlan:
    """Whole-range plan. With the segment_outline prompt in the session's snapshot: from outlines of 區塊
    (plan_by_outline). Otherwise one buffer at a time: the last unit of a buffer may continue past it, so it is not
    emitted: its blocks open the next buffer, refilled up to the limit. `done_windows` (window_id → saved result)
    lets a restarted job reuse finished buffers; `on_window(window_id, result)` is called after each buffer so the
    caller can persist it."""
    done_windows = done_windows or {}
    if deps.make_ctx is not None and "segment_outline" in deps.prompts and "segment_topic" in deps.prompts:
        try:
            return await plan_by_outline(blocks, deps, done_windows, on_window)
        except PlanInvalid as e:
            fallback = await plan_units(blocks, PlannerDeps(), done_windows={})
            fallback.warnings.insert(0, f"依區塊摘要切段失敗，改用規則切法：{e}")
            return fallback
    g = glues(blocks)
    all_groups: list[list[int]] = []
    reasons: list[str] = []
    infos, warnings = [], []
    pos = 0
    while pos < len(blocks):
        w = make_window(blocks, len(infos), pos, buffer_end(blocks, pos, g, window_chars, window_tokens))
        owned = [blocks[i].block_id for i in range(w.lo, w.hi)]
        saved = done_windows.get(w.window_id)
        if saved and saved.get("owned") == owned:
            groups, rs, info = saved["groups"], saved["reasons"], saved["info"]
        else:
            groups, rs, info = await plan_window(blocks, w, deps)
            if on_window:
                on_window(w.window_id, {"owned": owned, "groups": groups, "reasons": rs, "info": info})
        if w.hi < len(blocks) and len(groups) > 1:
            info = {**info, "carried": [blocks[i].block_id for i in groups[-1]]}
            groups, rs = groups[:-1], rs[:len(groups) - 1]
            pos = groups[-1][-1] + 1
        else:
            pos = w.hi
        all_groups += groups
        reasons += rs
        infos.append(info)
        warnings += info["notes"]
    errs = validate_groups(blocks, all_groups)
    if errs:   # cannot happen with validated buffers; never ship an unchecked plan
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
                            windows=infos, relations=glue_plan(blocks)[1])


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
    """Body text rebuilt from the snapshot. Headings before the body go to the heading field (leading_headings); a
    heading the planner kept inside a unit stays in the text as its own line. Sentences of one rebuilt PDF paragraph
    are joined into one paragraph; paragraphs are separated by a blank line."""
    from .source import _joiner

    out, group = "", None
    for b in blocks:
        if b.kind == "heading":
            if out:
                out += "\n\n" + b.text
                group = None
            continue
        g = b.meta.get("para_group")
        out += (_joiner(out, b.text) if g and g == group else "\n\n" if out else "") + b.text
        group = g
    return out


def leading_headings(blocks: list[SourceBlock]) -> list[str]:
    """Headings that open a unit, before its first body block."""
    out = []
    for b in blocks:
        if b.kind != "heading":
            break
        out.append(b.text)
    return out


def section_paths(blocks: list[SourceBlock]) -> dict[str, list[str]]:
    """block id → the path of formal sections (markdown headings of depth ≤ SECTION_LEVEL) it belongs to. A heading
    guessed from layout (PDF, structure_confidence low) never defines a section; deeper headings stay inside it."""
    stack: list[tuple[int, str]] = []
    out: dict[str, list[str]] = {}
    for b in blocks:
        if b.kind == "heading" and b.structure_confidence == "high" and b.heading_level and b.heading_level <= SECTION_LEVEL:
            stack = [x for x in stack if x[0] < b.heading_level] + [(b.heading_level, b.text)]
        out[b.block_id] = [t for _, t in stack]
    return out


def unit_chunks(u: SourceUnit, ordered: list[SourceBlock], pos: dict[str, int]) -> list[dict]:
    """The unit's 區塊 with their text: the planner's (with outlines), or, for a unit planned another way, grouped
    by the program now (no outline). A 區塊 of headings only has no text and is left out."""
    if u.chunks:
        raw = [(c.block_ids, c.topic, c.aspect, c.summary) for c in u.chunks]
    else:
        lo, hi = pos[u.block_ids[0]], pos[u.block_ids[-1]] + 1
        raw = [([ordered[i].block_id for i in c], "", "", "") for c in make_chunks(ordered, lo, hi)]
    out = []
    for ids, topic, aspect, summary in raw:
        bs = [ordered[pos[i]] for i in ids]
        text = unit_text(bs)
        # the unit's own leading headings are the quote's heading; a later 區塊 that opens with a subheading shows it
        # as its own heading (not as a sentence of its text: a heading is not a claim the guide must restate)
        heads = leading_headings(bs) if out else []
        if text:
            out.append({"block_ids": ids, "text": text, "heading": " › ".join(heads) or None, "topic": topic,
                        "aspect": aspect, "summary": summary})
    return out


def to_quotes(unit: str, plan: SegmentationPlan, by_id: dict[str, SourceBlock]) -> list[dict]:
    quotes = []
    pending_heading: str | None = None
    ordered = sorted(by_id.values(), key=lambda b: b.ordinal)
    paths = section_paths(ordered)
    pos = {b.block_id: k for k, b in enumerate(ordered)}
    related = {r["to"] for r in plan.relations}
    for u in plan.units:
        bs = [by_id[i] for i in u.block_ids]
        heads = leading_headings(bs)
        text = unit_text(bs)
        if not text:   # a unit of headings only (range ends on a heading): show it on the next / last quote
            pending_heading = " › ".join(filter(None, [pending_heading, *heads]))
            continue
        heading = " › ".join(filter(None, [pending_heading, *heads])) or None
        pending_heading = None
        body = [b for b in bs if b.kind != "heading"]
        quotes.append({"chunks": unit_chunks(u, ordered, pos), "id": f"quote-{len(quotes) + 1:02d}", "index": len(quotes), "label": _label(unit, bs),
                       "heading": heading, "excerpt": excerpt(text), "text": text, "chars": len(text), "part": 0,
                       "unit_id": u.unit_id, "block_ids": u.block_ids, "boundary_reason": u.boundary_reason,
                       "has_figure": any(b.kind == "figure" or (b.meta.get("pdf") or {}).get("caption") for b in bs),
                       "section": paths.get(body[0].block_id, []),
                       "structure_confidence": "low" if any(b.structure_confidence == "low" for b in ordered) else "high",
                       "depends_on_prev": body[0].block_id in related})
    if pending_heading and quotes:
        quotes[-1]["heading"] = " › ".join(filter(None, [quotes[-1]["heading"], pending_heading]))
    return quotes


def plan_parts(quotes: list[dict], part_chars: int = PART_CHARS) -> tuple[list[dict], list[ReadingPart]]:
    """Reading parts (legacy dict shape: index / quote_from / quote_to / chars / label, plus part_id, section, token
    estimate and boundary reason) and ReadingPart contracts. A part never spans two formal sections; within a
    section it closes at ~part_chars between complete units, never before a unit that depends on the previous one
    (a protected relation kept across units). Sets quote['part'] in place."""
    parts: list[dict] = []
    cur: dict | None = None
    for q in quotes:
        section = q.get("section") or []
        reason = ""
        if cur is None:
            reason = "範圍開頭"
        elif section != cur["section"]:
            reason = "新的小節：" + " › ".join(section[len([1 for a, b in zip(section, cur["section"]) if a == b]):] or section)
        elif cur["chars"] + q["chars"] > part_chars and not q.get("depends_on_prev"):
            reason = f"同一小節內超過約 {part_chars} 字，在完整單元之間分開"
        if reason:
            cur = {"index": len(parts), "quote_from": q["index"], "quote_to": q["index"], "chars": 0, "section": section,
                   "boundary_reason": reason, "structure_confidence": q.get("structure_confidence", "high")}
            parts.append(cur)
        cur["quote_to"] = q["index"]
        cur["chars"] += q["chars"]
        q["part"] = cur["index"]
    contracts = []
    for p in parts:
        qs = quotes[p["quote_from"]:p["quote_to"] + 1]
        a, b = qs[0]["label"], qs[-1]["label"]
        p["label"] = a if a == b else f"{a} 至 {b}"
        p["part_id"] = f"part-{p['index'] + 1:02d}"
        p["estimated_tokens"] = sum(estimate_tokens(q["text"]) for q in qs)
        p["estimation_method"] = TOKEN_METHOD
        contracts.append(ReadingPart(part_id=p["part_id"], index=p["index"], unit_ids=[q["unit_id"] for q in qs],
                                     estimated_tokens=p["estimated_tokens"], estimation_method=TOKEN_METHOD,
                                     section_path=p["section"], boundary_reason=p["boundary_reason"],
                                     structure_confidence=p["structure_confidence"]))
    return parts, contracts
