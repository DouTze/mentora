"""Program side of the handout review (reading-graph-v3).

A 區塊's handout is a teacher's handout, not a reprint: it must teach the points its 區塊 summary names, say nothing
the textbook contradicts, and keep the textbook's mathematics. Three checks, each with evidence the program can see:

- program checks (this module): the textbook's equations are written, KaTeX is balanced, no production narration,
  no repeated text, bold symbols of the PDF stay bold (sentences reused from the textbook are not counted: where the
  textbook is already clear, reusing it is fine);
- review A (points): a model says, per summary point, whether the handout teaches it, quoting the handout passage;
- review B (conflicts): a model lists contradictions — two passages that cannot both be true — quoting both the
  handout and the textbook passage.

A reviewer's claim counts only when its quotations are really in the texts; one that does not check out becomes
a note for the reader and never blocks. A problem is corrected in place (apply_edits): the corrector names the
passage it replaces, the program applies the edit only where that passage really is, and nothing else changes.

Nothing here decides that two sentences mean the same thing; the program decides what evidence is required and
whether the evidence offered is real."""
from __future__ import annotations

import re
import unicodedata

from . import agent as ra
from .source import sentence_cuts

LIST_MARK_RE = re.compile(r"\s*(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)、]\s*|\s*\(\w{1,3}\)\s*|\s*[-*+]\s*")
MATH_HINT_RE = re.compile(r"[∑∫√≤≥≠≈∞∂∇αβγδεθλμσπρφωΩΣΔΠ×÷±→⇒∈∉⊂⊆∀∃]|\b[a-zA-Z]\s*[=<>]\s*[\w(]|\^|_\{?\w")
# a handout narrating how it was made or corrected ("本次修訂…", "依照你的要求…"). Only such phrases: the bare
# words 使用者 / 這次 / 修訂 are subject matter too (NLU 判斷使用者意圖, 法規修訂).
PRODUCTION_RE = re.compile(r"(?:本次|這次)(?:修訂|修改|改寫|補充)|修訂(?:後的|版本|過程)|(?:根據|依照|依)(?:你|使用者)?的?(?:要求|指示)"
                           r"|(?:你|使用者)的(?:要求|指示)|為了回應|審稿|這裡補上剛才|以下為修正|修正後的版本")


# -- source segments ------------------------------------------------------------------------------

def source_segments(text: str) -> list[tuple[str, str]]:
    """[(S1, sentence), …] in reading order: blocks (blank-line separated) cut at sentence ends outside math;
    a display formula is its own segment. Segments with no letters / digits are skipped."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if not block:
            continue
        if block.startswith("$$") and block.endswith("$$"):
            out.append(block)
            continue
        cuts = [0] + sentence_cuts(block) + [len(block)]
        carry = ""
        for a, b in zip(cuts, cuts[1:]):
            seg = (carry + block[a:b]).strip() if carry else block[a:b].strip()
            carry = ""
            if LIST_MARK_RE.fullmatch(seg):   # "1." / "(a)" is a list marker, not a sentence: it opens the next one
                carry = block[a:b]
                continue
            if seg and re.search(r"\w", seg):
                out.append(seg)
        if carry.strip():
            out.append(carry.strip())
    return [(f"S{i}", s) for i, s in enumerate(out, 1)]


def evidence_formulas(evidence: dict | None) -> list[str]:
    """Formulas read from the rendered PDF regions (confirmed symbol evidence): the text layer lost them, so the
    handout writes them from here (required_formulas picks the equations among them)."""
    ev = evidence or {}
    if ev.get("status") != "confirmed":
        return []
    return [f.strip() for f in (ev.get("transcription") or {}).get("formulas", []) if f.strip()]


BOLD_WRAP_RE = r"\\(?:mathbf|boldsymbol|bm|mathbfit|pmb)\s*\{\s*%s\s*\}"


STYLE_WRAPS = {"bold": r"\mathbf{%s}", "italic": "%s", "calligraphic": r"\mathcal{%s}", "script": r"\mathscr{%s}",
               "blackboard": r"\mathbb{%s}", "fraktur": r"\mathfrak{%s}"}
STYLE_NAMES = {"bold": "粗體", "italic": "斜體", "calligraphic": "書法體", "script": "花體", "blackboard": "黑板粗體",
               "fraktur": "德文尖角體"}
WRAP_STYLES = {"mathbf": "bold", "boldsymbol": "bold", "bm": "bold", "pmb": "bold", "mathbfit": "bold",
               "mathcal": "calligraphic", "mathscr": "script", "mathbb": "blackboard", "mathfrak": "fraktur",
               "mathit": "italic"}
WRAPPED_LETTER_RE = re.compile(r"\\(%s)\s*\{\s*([A-Za-z])\s*\}" % "|".join(WRAP_STYLES))
BARE_LETTER_RE = re.compile(r"(?<![\\A-Za-z{])([A-Za-z])(?![A-Za-z])")


SYMBOLS_MAX = 30    # distinct symbols of one 區塊; a small model otherwise lists every occurrence until it is cut off
FORMULAS_MAX = 20


def symbols_schema() -> dict:
    from .contracts import SymbolTranscription
    sch = SymbolTranscription.model_json_schema()
    sch["properties"]["symbols"] = {**sch["properties"]["symbols"], "maxItems": SYMBOLS_MAX}
    sch["properties"]["formulas"] = {**sch["properties"]["formulas"], "maxItems": FORMULAS_MAX}
    sch["properties"]["unreadable"] = {**sch["properties"]["unreadable"], "maxItems": 10}
    return sch


def letter_fonts_text(letter_fonts: dict[str, list[str]]) -> str:
    """The font facts as the symbols prompt states them."""
    return "\n".join(f"- {k}：" + " 或 ".join(f"{STYLE_NAMES[s]}（{STYLE_WRAPS[s] % k}）" for s in v)
                     for k, v in letter_fonts.items()) or "（沒有可由字型確定的字母）"


def fix_by_fonts(tr, letter_fonts: dict[str, list[str]]):
    """(transcription, corrections, conflicts). The PDF's math fonts say which styles each letter was drawn in. A
    reading that writes a letter in a style its fonts never drew is corrected when the letter has only one style;
    with several (S_t and \\mathcal{S} in one region) only the rendered region tells which occurrence is which, so
    the reading stays a conflict. Repeated symbols and formulas are listed once."""
    fixes: list[str] = []
    conflicts: list[str] = []

    def restyle(letter: str, style: str) -> str | None:
        allowed = letter_fonts.get(letter)
        if not allowed or style in allowed:
            return None
        if len(allowed) == 1:
            fixes.append(f"{letter}：{STYLE_NAMES.get(style, style)} → {STYLE_NAMES[allowed[0]]}")
            return allowed[0]
        conflicts.append(f"字型顯示 {letter} 是{'或'.join(STYLE_NAMES[s] for s in allowed)}，"
                         f"視覺判讀寫成{STYLE_NAMES.get(style, style)}")
        return None

    def fix_math(text: str) -> str:
        def wrapped(m):
            new = restyle(m.group(2), WRAP_STYLES[m.group(1)])
            return STYLE_WRAPS[new] % m.group(2) if new else m.group(0)
        out = WRAPPED_LETTER_RE.sub(wrapped, text)
        parts = re.split(r"(\\[A-Za-z]+\s*(?:\{[^{}]*\})?)", out)   # a command and its argument are never bare letters
        for k in range(0, len(parts), 2):
            def bare(m):
                new = restyle(m.group(1), "italic")
                return STYLE_WRAPS[new] % m.group(1) if new else m.group(0)
            parts[k] = BARE_LETTER_RE.sub(bare, parts[k])
        return "".join(parts)

    formulas = list(dict.fromkeys(fix_math(f) for f in tr.formulas))
    symbols, seen = [], set()
    for x in tr.symbols:
        base = re.sub(r"[_^].*$", "", WRAPPED_LETTER_RE.sub(r"\2", x.symbol)).strip()
        if len(base) == 1 and base.isalpha():
            new = restyle(base, x.style)
            if new:
                x = x.model_copy(update={"style": new, "latex": fix_math(re.sub(WRAPPED_LETTER_RE, r"\2", x.latex))})
        key = (x.symbol, x.latex, x.style)
        if key not in seen:
            seen.add(key)
            symbols.append(x)
    return (tr.model_copy(update={"formulas": formulas, "symbols": symbols}), list(dict.fromkeys(fixes)),
            list(dict.fromkeys(conflicts)))


def transcription_against_clues(tr, clues: dict) -> list[str]:
    """Contradictions between a vision transcription and the font clues of the same regions (empty = consistent).
    Letters set in known math fonts are settled by fix_by_fonts; for the rest, flags and text fonts are only clues
    that can make a reading suspicious."""
    problems = []
    if tr.unreadable:
        problems.append("模型表示部分符號無法辨識：" + "、".join(tr.unreadable[:4]))
    known = set(clues.get("letter_fonts") or {})
    text = " ".join(tr.formulas + [x.latex for x in tr.symbols])
    bold_seen = {x.symbol for x in tr.symbols if x.style == "bold"}
    for t in clues.get("bold_tokens") or []:
        if len(t) == 1 and t.isalpha() and t not in known and t not in bold_seen \
                and not re.search(BOLD_WRAP_RE % re.escape(t), text):
            problems.append(f"字型顯示 {t} 為粗體，但轉寫沒有標成粗體")
    for t in clues.get("italic_tokens") or []:
        if len(t) == 1 and t.isalpha() and t not in known and any(x.symbol == t and x.style == "bold" for x in tr.symbols):
            problems.append(f"字型顯示 {t} 為斜體，但轉寫標成粗體")
    for t in clues.get("superscripts") or []:
        # a raised number is most often a footnote mark in prose ("…decision processes¹"), which no formula carries;
        # only a raised letter or symbol must show up as a superscript in the transcription
        if t.strip() and not t.strip().isdigit() and not re.search(r"\^\s*\{?\s*" + re.escape(t.strip()), text):
            problems.append(f"字型顯示 {t} 是上標，但轉寫沒有寫成上標")
    return problems


def notation_mismatches(evidence: dict, texts: list[str]) -> list[str]:
    """Where the handout writes a symbol the evidence shows in bold as a plain letter inside math."""
    bold = {x["symbol"] for x in (evidence.get("transcription") or {}).get("symbols", []) if x.get("style") == "bold"}
    math = " ".join(m for t in texts for m in re.findall(r"\$\$.+?\$\$|\$[^$\n]+\$", t or "", re.S))
    out = []
    for sym in sorted(bold):
        if len(sym) != 1:
            continue
        stripped = re.sub(BOLD_WRAP_RE % re.escape(sym), " ", math)
        if re.search(r"(?<![\\A-Za-z])" + re.escape(sym) + r"(?![A-Za-z])", stripped):
            out.append(f"依原書版面，{sym} 是粗體（寫成 \\mathbf{{{sym}}}），講義的公式寫成了一般字母")
    return out


def exact_ids_schema(model_cls, lists: dict[str, list[str]]) -> dict:
    """The model's JSON schema with each listed array restricted to exactly these ids (enum + item count), so
    constrained decoding cannot leave an expected check out. The Pydantic check still validates the result."""
    sch = model_cls.model_json_schema()
    defs = sch.get("$defs", {})
    for name, ids in lists.items():
        prop = sch["properties"][name]
        ref = prop["items"].get("$ref", "").split("/")[-1]
        item = dict(defs[ref]) if ref else dict(prop["items"])
        item["properties"] = {**item["properties"], "id": {"type": "string", "enum": ids or ["-"]}}
        sch["properties"][name] = {"type": "array", "items": item, "minItems": len(ids), "maxItems": len(ids)}
    return bound_strings(sch)


FREE_TEXT_MAX = 400   # chars of one free-text field of a check / audit answer: a reason, a note, a quoted fragment


def bound_strings(sch: dict, limit: int = FREE_TEXT_MAX) -> dict:
    """Every free-text string of a check / audit schema gets a maxLength (in place). A small model sometimes repeats
    one clause inside a reason until the token limit, leaving JSON that cannot be parsed; bounded, the string closes
    and the answer still parses. Quoted fragments are located by prefix, so a cut quote still points at its text."""
    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and not {"enum", "const", "maxLength"} & node.keys():
                node["maxLength"] = limit
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(sch)
    return sch


# Version of the handout review rules. A 區塊 handout accepted under other rules is not reused (the block cache and
# the durable per-node results of a guide operation carry it). The label is bumped on purpose; the hash of the
# modules that decide acceptance also changes it whenever that code changes, so a forgotten bump cannot reuse a
# stale verdict.
GATE_RULES_LABEL = "handout-v1"


def _verifier_version() -> str:
    import hashlib
    from pathlib import Path
    here = Path(__file__).parent
    h = hashlib.sha256()
    for name in ("gates.py", "contracts.py", "agent.py"):
        h.update((here / name).read_bytes())
    return f"{GATE_RULES_LABEL}+{h.hexdigest()[:12]}"


GATE_RULES_VERSION = _verifier_version()


def symbol_like(text: str) -> bool:
    return bool(MATH_HINT_RE.search(text or "")) or "$" in (text or "")


# -- the points a 區塊's handout must teach -------------------------------------------------------------------

def handout_points(t: ra.ReadingTaskInput) -> list[tuple[str, str]]:
    """[(P1, point)]: what the 區塊's summary (Step 1) says it teaches. A 區塊 without a summary (a rule-planned
    unit) falls back to its 主題's overview; with neither there is nothing to check coverage against."""
    tp = t.topic or {}
    chunks = tp.get("chunks") or []
    k = int(tp.get("index", 0))
    if 0 <= k < len(chunks):
        c = chunks[k]
        head = "｜".join(x for x in (c.get("topic", "").strip(), c.get("aspect", "").strip()) if x)
        summary = c.get("summary", "").strip()
        if summary:
            return [("P1", f"（{head}）{summary}" if head else summary)]
    if (tp.get("summary") or "").strip():
        return [("P1", tp["summary"].strip())]
    return []


def handout_tokens(source: str) -> int:
    """Output reserved for a handout: about twice the textbook passage, 2048–4096 tokens."""
    from ..llm.context import estimate_tokens
    return max(2048, min(4096, 2 * estimate_tokens(source) + 512))


def handout_schema(source: str) -> dict:
    """Constrained-decoding schema of a handout with bounded strings. A small model sometimes falls into repeating
    "\\n" until the token limit, leaving JSON that cannot be parsed; with a length bound the string is closed and the
    (useless) handout is caught by the checks and corrected instead of wasting a format fix."""
    from .contracts import HandoutDraft
    sch = HandoutDraft.model_json_schema()
    props = dict(sch["properties"])
    props["guide"] = {**props["guide"], "maxLength": int(handout_tokens(source) * 0.8)}
    props["key_points"] = {"type": "array", "items": {"type": "string", "maxLength": 160}, "maxItems": 3}
    props["check"] = {**props["check"], "maxLength": 240}
    sch["properties"] = props
    return sch


def points_text(points: list[tuple[str, str]]) -> str:
    return "\n".join(f"- {i}：{p}" for i, p in points) or "（沒有區塊摘要：依原文教這一段的內容）"


def points_template(points: list[tuple[str, str]]) -> str:
    import json
    return json.dumps({"points": [{"id": i, "taught": "yes", "guide_quote": "…"} for i, _ in points]}, ensure_ascii=False)


def points_findings(points: list[tuple[str, str]], review, guide: str) -> list[ra.Finding]:
    """Review A → findings. "no" is the reviewer's own verdict on the handout: a point to add. A "yes" whose quotation
    is not in the handout is recorded (unconfirmed) and blocks nothing."""
    text = dict(points)
    out = []
    for v in review.points:
        p = text.get(v.id)
        if p is None:
            continue
        if v.taught == "no":
            out.append(ra.Finding(code="point_missing", check="points", severity="major", item=v.id,
                                  detail=f"講義沒有教到這個重點：{p}", suggested_fix=p))
        elif not ra.contains_quote(guide, v.guide_quote, 4):
            out.append(ra.Finding(code="point_unconfirmed", check="points", severity="minor", status="unconfirmed",
                                  item=v.id, detail=f"審稿說有教到「{p[:40]}」，但引用的句子不在講義中"))
    return out


# -- conflicts with the textbook ------------------------------------------------------------------------------

def conflict_findings(review, guide: str, source: str) -> list[ra.Finding]:
    """Review B → findings. A contradiction is two passages that cannot both be true; one the reviewer itself says
    can both be true (a rewording, an addition) is dropped. It counts when both quotations are real: the handout
    passage and the textbook passage it contradicts. Otherwise it is a note for the reader ("may not match")."""
    out = []
    for c in review.conflicts:
        if c.both_true == "yes":
            continue
        g = ra.expand_quote(guide, c.guide_quote, 4)
        s = ra.expand_quote(source, c.source_quote, 4)
        why = (c.why or "").strip()
        if g and s:
            out.append(ra.Finding(code="conflict", check="conflicts", severity="major", draft_excerpt=c.guide_quote.strip(),
                                  source_excerpt=s, detail=f"講義「{g[:80]}」和課本「{s[:80]}」矛盾" + (f"：{why}" if why else "")))
        elif c.guide_quote.strip():
            out.append(ra.Finding(code="conflict_unconfirmed", check="conflicts", severity="minor", status="unconfirmed",
                                  draft_excerpt=c.guide_quote.strip()[:120], source_excerpt=c.source_quote.strip()[:120],
                                  detail="審稿認為這裡可能和課本不一致，但引用的句子對不上原文或講義" + (f"：{why}" if why else "")))
    return out


# -- program checks ---------------------------------------------------------------------------------------------

# A handout may reuse the textbook's own sentences where they are already clear: whether a passage needed explaining
# is a judgement about meaning, so no program check counts copied text.
EQUATION_RE = re.compile(r"=|<|>|\\(?:doteq|triangleq|approx|equiv|propto|leftarrow|gets|rightarrow|to|leq?|geq?|neq|sim)\b")


def required_formulas(t: ra.ReadingTaskInput, symbol_evidence: dict | None) -> list[str]:
    """The textbook's equations a handout must write: its display formulas, and the equations read from the PDF
    region (a lone symbol such as S_t is notation, not an equation to teach)."""
    display, _ = ra.source_formulas(t.text)
    ev = [ra._norm_math(f) for f in evidence_formulas(symbol_evidence) if EQUATION_RE.search(f) and len(f) >= 6]
    return list(dict.fromkeys(display + ev))


def repeated_sentences(text: str, times: int = 3, min_chars: int = 10) -> list[str]:
    """Sentences written `times` or more times (a small model looping), ignoring code and formulas."""
    from collections import Counter
    prose = re.sub(r"```.*?```|\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`", " ", text or "", flags=re.S)
    sents = [x.strip(" \n*") for x in re.split(r"[。！？!?\n]", prose)]
    return [x for x, n in Counter(x for x in sents if len(x) >= min_chars).items() if n >= times]


def _sentence_of(text: str, start: int) -> str:
    a = max(text.rfind(ch, 0, start) for ch in "。！？!?\n") + 1
    ends = [i for i in (text.find(ch, start) for ch in "。！？!?\n") if i != -1]
    return text[a:(min(ends) + 1 if ends else len(text))].strip()


def handout_program(t: ra.ReadingTaskInput, guide: str, symbol_evidence: dict | None = None) -> list[ra.Finding]:
    """Program checks of a handout. Every finding is a fact the program sees; each is corrected in place."""
    g = guide or ""
    out: list[ra.Finding] = []

    def add(code: str, detail: str, excerpt: str = "", fix: str = "") -> None:
        out.append(ra.Finding(code=code, check="program", severity="major", detail=detail, draft_excerpt=excerpt,
                              suggested_fix=fix))

    if len(g.strip()) < 10:
        add("empty_guide", "講義是空的")
        return out
    if (g.count("$") - 2 * g.count("$$")) % 2 or g.count("$$") % 2:
        add("broken_math", "講義的 $ 數量不成對，公式會無法顯示")
    gn = ra._norm_math(g)
    for f in required_formulas(t, symbol_evidence):
        if f not in gn:
            add("formula_missing", f"講義沒有寫出課本的公式 $${f}$$（要寫出來並說明式中符號）", fix=f"$${f}$$")
    m = PRODUCTION_RE.search(g)
    if m:
        add("process_leak", f"講義敘述了寫作或修改過程：「{m.group(0)}」", excerpt=_sentence_of(g, m.start()))
    for s in repeated_sentences(g)[:1]:
        add("repeated_text", f"講義同一句重複了三次以上：「{s[:40]}」", excerpt=s)
    if symbol_evidence:
        for d in notation_mismatches(symbol_evidence, [g]):
            add("notation_mismatch", d)
    return out


# -- what the handout tells the reader it could not settle ---------------------------------------------------------

NOTE_TEXT = {
    "point_missing": lambda f: f"這段講義可能沒有教到：{f.suggested_fix[:80]}",
    "conflict_unconfirmed": lambda f: f"這段講義的「{f.draft_excerpt[:60]}」可能和課本不一致，建議對照課本",
    "formula_missing": lambda f: f"這段講義沒有寫出課本的公式 {f.suggested_fix[:80]}",
    "notation_mismatch": lambda f: f.detail[:100],
    "notation_unverified": lambda f: f"PDF 的數學符號沒能確認字型，符號寫法建議對照課本：{f.detail[:80]}",
}


ALWAYS_NOTED = {"conflict_unconfirmed", "notation_unverified"}   # never blocking, always worth telling the reader


def reader_notes(findings: list[ra.Finding]) -> list[dict]:
    """Notes published with the handout: what is still open after the corrections (a contradiction is never
    published; a point or a program fact left open, a contradiction the reviewer could not quote, or symbols that
    could not be confirmed, are)."""
    out = []
    for f in findings:
        make = NOTE_TEXT.get(f.code)
        if make and (f.severity == "major" or f.code in ALWAYS_NOTED):
            out.append({"kind": "note", "text": make(f), "code": f.code})
    return out


# -- correction in place --------------------------------------------------------------------------------------------

def problems_text(findings: list[ra.Finding]) -> str:
    """The problem list a correction answers, numbered F1, F2 …"""
    lines = []
    for k, f in enumerate(findings, 1):
        what = {"points": "漏教重點", "conflicts": "和課本矛盾", "program": "講義問題"}[f.check]
        line = f"- F{k}［{what}］{f.detail}"
        if f.draft_excerpt:
            line += f"\n  講義中的這段：「{f.draft_excerpt[:300]}」"
        if f.source_excerpt and f.check == "conflicts":
            line += f"\n  課本的原句：「{f.source_excerpt[:300]}」"
        if f.check == "points":
            line += "\n  → 用 insert_after 補上這個重點的講解。"
        elif f.code == "formula_missing":
            line += f"\n  → 用 insert_after 補上公式 {f.suggested_fix} 並說明式中符號。"
        elif f.draft_excerpt:
            line += "\n  → 用 replace 改寫這段。"
        lines.append(line)
    return "\n".join(lines)


def _locate(text: str, quote: str) -> tuple[int, int] | None:
    """Where `quote` is in `text`, as is or ignoring whitespace, Markdown marks and character width."""
    q = (quote or "").strip()
    if not q:
        return None
    i = text.find(q)
    if i != -1:
        return i, i + len(q)
    chars, where = [], []
    for k, ch in enumerate(text):
        if re.match(r"[\s*_`>]", ch):
            continue
        for c in unicodedata.normalize("NFKC", ch):
            chars.append(c)
            where.append(k)
    flat, nq = "".join(chars), ra.norm_text(q).strip("「」\"'")
    i = flat.find(nq) if nq else -1
    if i == -1:
        return None
    return where[i], where[i + len(nq) - 1] + 1


def apply_edits(guide: str, edits: list) -> tuple[str, list[dict], list[str]]:
    """(new handout, edits applied with the passage each produced, edits skipped and why). An edit applies only
    where its passage really is; everything else in the handout stays as it was."""
    text, applied, skipped = guide, [], []
    for e in edits:
        new = (e.new or "").strip()
        if e.action == "replace":
            span = _locate(text, e.old)
            if span is None:
                skipped.append(f"{e.problem or '?'}：要換掉的句子不在講義中")
                continue
            a, b = span
            text = text[:a] + new + text[b:]
        else:
            if not new:
                skipped.append(f"{e.problem or '?'}：沒有要補上的內容")
                continue
            if (e.old or "").strip():
                span = _locate(text, e.old)
                if span is None:
                    skipped.append(f"{e.problem or '?'}：要接在後面的句子不在講義中")
                    continue
                end = text.find("\n\n", span[1])
                a = len(text) if end == -1 else end
            else:
                a = len(text)
            text = text[:a].rstrip() + "\n\n" + new + ("\n\n" + text[a:].lstrip() if text[a:].strip() else "")
        applied.append({"problem": e.problem, "action": e.action, "passage": new})
    return re.sub(r"\n{3,}", "\n\n", text).strip(), applied, skipped
