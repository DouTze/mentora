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

import json
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


LETTER_RE = re.compile(r"(?<![\\A-Za-z])[A-Za-z](?![A-Za-z])")
TEXT_ARG_RE = re.compile(r"\\(?:text|mathrm|operatorname)\s*\{[^{}]*\}")


def _signs(text: str) -> tuple[set[str], set[str]]:
    """(single Latin letters, other signs as LaTeX commands) written in `text`: inside its $…$ or as prose
    characters (η, ∇)."""
    from ..pdfdoc.math import uni_tex
    t = TEXT_ARG_RE.sub(" ", text or "")
    cmds = set(re.findall(r"\\([A-Za-z]+)", t)) | {uni_tex(c)[1:] for c in t if ord(c) > 127 and uni_tex(c).startswith("\\")}
    return set(LETTER_RE.findall(re.sub(r"\\[A-Za-z]+", " ", t))), cmds


def anchored(tr, source: str):
    """The transcription without what the passage does not have: a formula is the passage's when each of its
    letters is in the passage's text (the text layer loses a letter's style, rarely the letter) or, written with
    no letter, one of its signs is. A small model reading an image sometimes answers with the prompt's example or
    a formula from elsewhere; required from the handout, it would be taught as the textbook's."""
    letters, cmds = _signs(source)

    def ours(latex: str) -> bool:
        ls, cs = _signs(latex)
        return ls <= letters if ls else bool(cs & cmds)
    return tr.model_copy(update={"formulas": [f for f in tr.formulas if ours(f)],
                                 "symbols": [x for x in tr.symbols if ours(x.latex)]})


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
GATE_RULES_LABEL = "handout-v2"


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


REVIEW_B_RUNS = 2   # review B reads each handout twice; a paragraph either run finds in conflict is corrected. On recorded
#                     handouts one run caught 3–5 of 7 planted contradictions, the two runs together 6, with no more
#                     false alarms (1 of 34 handouts) than one run
DISPLAY_ONLY_RE = re.compile(r"^\s*\$\$.*\$\$\s*$", re.S)


def review_paragraphs(text: str) -> list[str]:
    """The handout paragraphs review B checks: blank-line separated, a paragraph that is only a display formula left
    to the program checks (whether a formula is written right is a fact the program compares, not a claim)."""
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip() and not DISPLAY_ONLY_RE.match(p)]


def paragraphs_text(paras: list[str]) -> str:
    return "\n\n".join(f"【P{k}】\n{p}" for k, p in enumerate(paras, 1))


def paragraphs_template(n: int) -> str:
    return json.dumps({"paragraphs": [{"id": f"P{k}", "guide_says": "…", "source_quote": "…", "source_says": "…",
                                       "relation": "…"} for k in range(1, n + 1)]}, ensure_ascii=False)


def paragraphs_schema(n: int) -> dict:
    """One verdict per paragraph, in order, with bounded strings."""
    item = {"type": "object", "properties": {
        "id": {"type": "string", "enum": [f"P{k}" for k in range(1, n + 1)]},
        "guide_says": {"type": "string", "maxLength": FREE_TEXT_MAX},
        "source_quote": {"type": "string", "maxLength": FREE_TEXT_MAX},
        "source_says": {"type": "string", "maxLength": FREE_TEXT_MAX},
        "relation": {"type": "string", "enum": ["consistent", "addition", "conflict"]}},
        "required": ["id", "guide_says", "source_quote", "source_says", "relation"]}
    return {"type": "object", "properties": {"paragraphs": {"type": "array", "items": item, "minItems": n, "maxItems": n}},
            "required": ["paragraphs"]}


def paragraph_findings(reviews: list, paras: list[str], source: str) -> list[ra.Finding]:
    """Review B's runs → findings, one per paragraph. A conflict is two passages that cannot both be true: it counts
    when the textbook sentence it names is really in the textbook; a conflict with no such sentence is a note for the
    reader ("may not match"). Either run naming a paragraph is enough."""
    found: dict[int, ra.Finding] = {}
    for review in reviews:
        for c in review.paragraphs:
            k = int(c.id[1:]) - 1 if re.fullmatch(r"P\d+", c.id or "") else -1
            if c.relation != "conflict" or not 0 <= k < len(paras):
                continue
            s = ra.expand_quote(source, c.source_quote, 4) if c.source_quote.strip() else ""
            said = f"講義說「{c.guide_says.strip()}」" + (f"，課本說「{c.source_says.strip()}」" if c.source_says.strip() else "")
            if s:
                found[k] = ra.Finding(code="conflict", check="conflicts", severity="major", draft_excerpt=paras[k],
                                      source_excerpt=s, detail=f"{said}，兩者不可能同時成立")
            elif k not in found:
                found[k] = ra.Finding(code="conflict_unconfirmed", check="conflicts", severity="minor", status="unconfirmed",
                                      draft_excerpt=paras[k][:120], detail=f"審稿認為這段可能和課本不一致，但找不到對應的課本原句：{said}")
    return [found[k] for k in sorted(found)]


def fix_schema(source: str, problems: int) -> dict:
    """A correction: one edit per problem; `old` is the passage to find (a sentence or two), `new` may be longer."""
    from .contracts import HandoutFix
    sch = bound_strings(HandoutFix.model_json_schema(), handout_chars(source))
    sch["properties"]["edits"] = {**sch["properties"]["edits"], "maxItems": max(1, problems)}
    return sch


def handout_tokens(source: str) -> int:
    """Output reserved for a handout: about three times the textbook passage plus room for its explanations,
    2048–5120 tokens. A handout explains more than its passage says (a short passage often three to five times over);
    cut off at the limit, its JSON does not parse and the whole 導讀段 is lost."""
    from ..llm.context import estimate_tokens
    return max(2048, min(5120, 3 * estimate_tokens(source) + 1024))


def handout_chars(source: str) -> int:
    """The longest handout (in characters) that fits 80 % of its token budget. A bound in characters means another
    length in each language: a CJK character is about a token, a Latin one a third or less (estimate_tokens), so the
    bound is converted with the passage's own characters per token."""
    from ..llm.context import chars_per_token
    return int(handout_tokens(source) * 0.8 * chars_per_token(source))


WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
LATIN_WORD_CHARS = 1.7     # a Latin word says about as much as this many Chinese characters
HANDOUT_PER_SOURCE = 2     # a handout explains its passage in about twice its length …
HANDOUT_MIN_ZH = 250       # … and a passage of a sentence or two still in a short paragraph or two
HANDOUT_MIN_WORDS = 120


def handout_length(text: str, language: str) -> str:
    """How long the handout should be, as the model can count it: Chinese characters or Latin words of the output
    language. Told only to keep it short, a small model writes a sentence of the textbook into thousands of
    characters, repeating itself and inventing what the passage never says."""
    cjk = sum(len(m) for m in CJK_SCRIPT_RE.findall(text or ""))
    words = len(WORD_RE.findall(re.sub(r"\$\$.+?\$\$|\$[^$\n]+\$", " ", text or "", flags=re.S)))
    size = cjk + LATIN_WORD_CHARS * words
    if language != "src" or cjk > words:
        src = f"約 {cjk} 字" if cjk > words else f"約 {words} 個英文單字"
        return f"這一段課文{src}，講義寫到約 {max(HANDOUT_MIN_ZH, round(HANDOUT_PER_SOURCE * size, -1)):.0f} 字以內就夠。"
    return (f"這一段課文約 {words} 個單字，講義寫到約 "
            f"{max(HANDOUT_MIN_WORDS, round(HANDOUT_PER_SOURCE * words, -1)):.0f} 個單字以內就夠。")


def handout_schema(source: str) -> dict:
    """Constrained-decoding schema of a handout with bounded strings. A small model sometimes falls into repeating
    "\\n" until the token limit, leaving JSON that cannot be parsed; with a length bound the string is closed and the
    (useless) handout is caught by the checks and corrected instead of wasting a format fix."""
    from .contracts import HandoutDraft
    sch = HandoutDraft.model_json_schema()
    props = dict(sch["properties"])
    from ..llm.context import chars_per_token
    scale = chars_per_token(source)
    props["guide"] = {**props["guide"], "maxLength": handout_chars(source)}
    props["key_points"] = {"type": "array", "items": {"type": "string", "maxLength": int(160 * scale)}, "maxItems": 3}
    props["check"] = {**props["check"], "maxLength": int(240 * scale)}
    sch["properties"] = props
    return sch


def intent_schema(source: str) -> dict:
    """What a 導讀段 teaches: a role and 1–3 one-sentence items, bounded in the source's own language."""
    from ..llm.context import chars_per_token
    from .contracts import TeachingIntent
    scale = chars_per_token(source)
    sch = TeachingIntent.model_json_schema()
    sch["properties"]["role"] = {**sch["properties"]["role"], "maxLength": int(120 * scale)}
    sch["properties"]["teaches"] = {"type": "array", "items": {"type": "string", "maxLength": int(120 * scale)},
                                    "minItems": 1, "maxItems": 3}
    sch["required"] = ["role", "teaches"]
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

# -- program checks ---------------------------------------------------------------------------------------------

# A handout may reuse the textbook's own sentences where they are already clear: whether a passage needed explaining
# is a judgement about meaning, so no program check counts copied text.
# the handout answer's other fields (HandoutDraft) named in its text, as a model writes them when they run together
OUTPUT_FIELD_RE = re.compile(r"[\"']?\b(key_points)\b[\"']?\s*[:：]|[\"'](check)[\"']\s*[:：]"
                             r"|\*\*\s*(key[ _]points|check)\s*(?:\*\*\s*[:：]|[:：]\s*\*\*)"   # also as a bold label
                             r"|^[ \t]*(key[ _]points(?:[ \t]+\w+){0,3}|check)[ \t]*[:：]", re.I | re.M)   # or opening a paragraph
CJK_SCRIPT_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+")
EQUATION_RE = re.compile(r"=|<|>|\\(?:doteq|triangleq|approx|equiv|propto|leftarrow|gets|rightarrow|to|leq?|geq?|neq|sim)\b")


FORMULA_PUNCT_RE = re.compile(r"[.,;:]+(?=\\tag\{|\$|$)")


def formula_bare(f: str) -> str:
    """A normalized formula without the sentence's punctuation a display formula closes with ("…(s'|s,a). \\tag{3.6}")
    and without its equation number: a handout writing the formula inside its own sentence leaves the punctuation out
    or puts it elsewhere, and the equation is written whether or not it carries the textbook's number."""
    return TAG_RE.sub("", FORMULA_PUNCT_RE.sub("", f))


LONE_SIGN_RE = re.compile(r"\\[A-Za-z]+|.")


def required_formulas(t: ra.ReadingTaskInput, symbol_evidence: dict | None) -> list[str]:
    """The textbook's equations a handout must write: its display formulas, and the equations read from the PDF
    region (a lone symbol such as S_t is notation, not an equation to teach)."""
    import difflib
    display = [f for f in ra.source_formulas(t.text)[0] if not LONE_SIGN_RE.fullmatch(f)]   # an end mark (\square) is no formula
    ev = [ra._norm_math(f) for f in evidence_formulas(symbol_evidence) if EQUATION_RE.search(f) and len(f) >= 6]
    def key(f: str) -> str:   # spacing, $ delimiters, rows of an aligned display and \text{\gamma} wrappers aside
        f = re.sub(r"\\(?:text|mathrm)\{(\\[A-Za-z]+)\}", r"\1", formula_bare(f))
        return re.sub(r"\\\\|&|\s|\$|\\(?:begin|end)\{[a-z]+\*?\}", "", f).rstrip("=.,")

    def rows(d: str) -> list[str]:
        """An aligned display read one row at a time (its left side and each row), and a chain of equalities one link
        at a time (G = Σ γ^k = 1/(1−γ) also says G = 1/(1−γ))."""
        parts = re.split(r"\\\\", d)
        lhs = parts[0].split("&", 1)[0] if "&" in parts[0] else ""
        out = [d] + ([lhs + p for p in parts] if len(parts) > 1 else [])
        links = [x for x in key(d).split("=") if x]
        return out + [links[0] + "=" + x for x in links[1:]]
    bare = [key(r) for d in display + ra.source_formulas(t.text)[1] for r in rows(d)]

    def own(e: str) -> bool:
        """A formula the region shows that the passage already sets (read in part, a row of it, or with slips) is that
        formula."""
        b = key(e)
        return any(b in d or difflib.SequenceMatcher(None, b, d, autojunk=False).ratio() >= COPY_MIN_RATIO for d in bare)
    return list(dict.fromkeys(display + [e for e in ev if not own(e)]))


def repeated_sentences(text: str, times: int = 3, min_chars: int = 10) -> list[str]:
    """Sentences written `times` or more times (a small model looping), ignoring code and formulas."""
    from collections import Counter
    prose = re.sub(r"```.*?```|\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`", " ", text or "", flags=re.S)
    sents = [x.strip(" \n*") for x in re.split(r"[。！？!?\n]", prose)]
    return [x for x, n in Counter(x for x in sents if len(x) >= min_chars).items() if n >= times]


RESTATED_MIN = 0.85   # two sentences this alike, word for word, say the same thing twice


def restated_sentences(text: str, min_chars: int = 40) -> list[str]:
    """Sentences that say again, nearly word for word, what an earlier sentence of the handout said (formulas inside
    a sentence count as words; display formulas and code are left out)."""
    import difflib
    prose = re.sub(r"```.*?```|\$\$.+?\$\$", " ", text or "", flags=re.S)
    sents = [x.strip(" \n*-") for x in re.split(r"(?<=[.!?。！？])\s+|\n+", prose)]
    sents = [x for x in sents if len(x) >= min_chars]
    words = [re.findall(r"\$[^$]+\$|\w+", x.lower()) for x in sents]
    out = []
    for j in range(1, len(sents)):
        if any(difflib.SequenceMatcher(None, words[i], words[j], autojunk=False).ratio() >= RESTATED_MIN for i in range(j)):
            out.append(sents[j])
    return out


DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")


def drop_repeats(text: str, times: int = 3) -> str:
    """The handout without what a small model wrote over again in a loop: a paragraph the same as an earlier one, and
    a display formula written `times` or more times, kept where it is first written (a copy standing as its own
    paragraph is removed, a copy inside a sentence stays)."""
    from collections import Counter
    seen = Counter(ra._norm_math(m.group(1)) for m in DISPLAY_RE.finditer(text or ""))
    loops = {f for f, n in seen.items() if n >= times}
    kept, out = set(), []
    for para in PARAGRAPH_BREAK_RE.split(text or ""):
        m = DISPLAY_RE.fullmatch(para.strip())
        key = ra._norm_math(m.group(1)) if m and ra._norm_math(m.group(1)) in loops else ra._norm_math(para.strip(" *"))
        if key and key in kept:
            continue
        kept.add(key)
        out.append(para)
    return "\n\n".join(out).strip()


COPY_MIN_RATIO = 0.85   # a display formula this close to one of the textbook's is a copy of it with slips
TAG_RE = re.compile(r"\\tag\{([^{}]*)\}")


def restore_formulas(text: str, source: str) -> str:
    """The handout with each slipped copy of a textbook display formula replaced by the textbook's own. A small model
    copying a formula through JSON loses commands on the way (\\doteq → \\buteq, \\mathbb{E} → \\bbE); asked to write
    it again, it slips again, and every correction adds another broken copy. A copy is the formula carrying a textbook
    equation's number, or, unnumbered, one close to a textbook formula; a formula with a number the textbook passage
    does not have, or not close to any, is the handout's own and stays."""
    import difflib
    originals = {ra._norm_math(f): f.strip() for f in DISPLAY_RE.findall(source or "") if f.strip()}
    if not originals:
        return text

    numbered = {tuple(TAG_RE.findall(o)): o for o in originals if TAG_RE.findall(o)}

    def restored(m):
        f = ra._norm_math(m.group(1))
        if f in originals:
            return m.group(0)
        if TAG_RE.findall(f):   # an equation number names the textbook's equation: a formula numbered so is a copy of it
            o = numbered.get(tuple(TAG_RE.findall(f)))
            return f"$${originals[o]}$$" if o else m.group(0)
        best = max(originals, key=lambda o: difflib.SequenceMatcher(None, f, o, autojunk=False).ratio())
        close = difflib.SequenceMatcher(None, f, best, autojunk=False).ratio() >= COPY_MIN_RATIO
        return f"$${originals[best]}$$" if close else m.group(0)
    return DISPLAY_RE.sub(restored, text or "")


def strip_field_leaks(text: str) -> str:
    """The handout without another field of the answer written into its text (`key_points: [...]`, `"check": "…"`):
    from the field's name to the end of its paragraph, nothing belongs to the handout."""
    while (m := OUTPUT_FIELD_RE.search(text or "")):
        end = text.find("\n\n", m.end())
        text = text[:m.start()].rstrip(" \t\"',{[") + ("\n\n" + text[end:].lstrip() if end != -1 else "")
    return (text or "").strip()


def place_formulas(text: str, source: str) -> str:
    """The handout with each textbook display formula it has not written in full written in by the program, as a
    display after the paragraph whose math is closest to it (where the handout talks about it; after the first
    paragraph when no math is close). A small model writes a long formula in part (its right-hand side, = for \\doteq)
    and, asked to write it in full, adds another copy and another explanation each time it tries."""
    import difflib
    paras = [p for p in PARAGRAPH_BREAK_RE.split(text or "") if p.strip()]
    if not paras:
        return text
    after: dict[int, list[str]] = {}
    for raw in DISPLAY_RE.findall(source or ""):
        f = ra._norm_math(raw)
        if LONE_SIGN_RE.fullmatch(f) or formula_bare(f) in formula_bare(ra._norm_math("\n".join(paras))):
            continue

        def closeness(k: int) -> float:
            maths = [ra._norm_math(a or b) for a, b in re.findall(r"\$\$(.+?)\$\$|\$([^$\n]+)\$", paras[k], re.S)]
            return max((difflib.SequenceMatcher(None, m, f, autojunk=False).ratio() for m in maths), default=0.0)
        k = max(range(len(paras)), key=closeness)
        after.setdefault(k if closeness(k) >= 0.5 else 0, []).append(f"$${raw.strip()}$$")
    if not after:
        return text
    return "\n\n".join(p + "".join("\n\n" + f for f in after.get(k, [])) for k, p in enumerate(paras)).strip()


def tidy_handout(text: str, source: str) -> str:
    """What the program corrects itself, after the draft and after every correction: facts it sees exactly, which a
    small model asked to correct them gets wrong again and again (spending every correction on them)."""
    text = re.sub(r"\${3,}", "$$", strip_field_leaks(text))   # $$$…$$$: a display around an inline formula
    return drop_repeats(place_formulas(restore_formulas(text, source), source))


SENTENCE_END_RE = re.compile(r"[。！？\n]|[.!?](?=\s|$)")


def _sentence_of(text: str, start: int) -> str:
    """The sentence of `text` around position `start` (Chinese or Latin sentence ends)."""
    a = max((m.end() for m in SENTENCE_END_RE.finditer(text, 0, start)), default=0)
    m = SENTENCE_END_RE.search(text, start)
    return text[a:(m.end() if m else len(text))].strip()


# a letter-like sign: a Greek letter or a quantifier. A handout writing one the passage never uses has put another
# symbol in its place (γ written as \forall), or teaches notation the passage does not
SYMBOL_SIGNS = frozenset("""alpha beta gamma Gamma delta Delta epsilon varepsilon zeta eta theta Theta vartheta iota kappa
lambda Lambda mu nu xi Xi pi Pi rho varrho sigma Sigma tau upsilon phi Phi varphi chi psi Psi omega Omega forall
exists mathcal mathscr mathbb mathfrak""".split())   # and a letter style the passage does not set its sets in (𝒮 as \mathcal)
# an operator LaTeX has, written as a word: \text{sum} for \sum
OPERATOR_WORD_RE = re.compile(r"\\(?:text|mathrm|operatorname)\s*\{(sum|prod|int|lim|max|min|log|exp|sup|inf|Pr)\}")
MATH_RE = re.compile(r"\$\$(.+?)\$\$|\$([^$\n]+)\$", re.S)


def foreign_signs(guide: str, source: str) -> list[str]:
    """Letter-like signs (SYMBOL_SIGNS) the handout's math writes and the passage (with what it cites) never does."""
    _, own = _signs(source)
    used = [c for m in MATH_RE.finditer(guide or "") for c in re.findall(r"\\([A-Za-z]+)", m.group(1) or m.group(2))]
    return [c for c in dict.fromkeys(used) if c in SYMBOL_SIGNS and c not in own]


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
    gn = formula_bare(ra._norm_math(g))
    for f in required_formulas(t, symbol_evidence):
        if formula_bare(f) not in gn:   # the sentence punctuation left out on both sides
            add("formula_missing", f"講義沒有寫出課本的公式 $${f}$$（要寫出來並說明式中符號）", fix=f"$${f}$$")
    m = PRODUCTION_RE.search(g)
    if m:
        add("process_leak", f"講義敘述了寫作或修改過程：「{m.group(0)}」", excerpt=_sentence_of(g, m.start()))
    m = OUTPUT_FIELD_RE.search(g)
    if m:   # another field of the answer written into the handout's text: the fields ran together
        a, b = g.rfind("\n", 0, m.start()) + 1, g.find("\n\n", m.end())
        add("field_leak", f"講義正文裡寫進了輸出格式的欄位「{next(g for g in m.groups() if g)}」，那部分不屬於講義，要刪除",
            excerpt=g[a:b if b != -1 else len(g)].strip())
    for s in repeated_sentences(g)[:1]:
        add("repeated_text", f"講義同一句重複了三次以上：「{s[:40]}」", excerpt=s)
    for s in restated_sentences(g)[:2]:
        add("repeated_text", f"這一句和講義前面的一句幾乎一樣，重複了：「{s[:60]}」，刪掉這一句", excerpt=s)
    for m in list(dict.fromkeys(OPERATOR_WORD_RE.findall(g)))[:2]:
        add("math_word", f"講義的數學式把運算子寫成文字 \\text{{{m}}}，要寫成 \\{m}",
            excerpt=_sentence_of(g, g.find("{" + m + "}")))
    # the passage (with the formulas read from its PDF region: the text layer loses signs), the formulas it cites, the
    # passage before it and what the reader has read: notation from there is the textbook's
    own_text = "\n".join([t.text, *required_formulas(t, symbol_evidence)])
    known_text = "\n".join([own_text, *t.cited, ((t.topic or {}).get("prev") or {}).get("text") or "", *t.prev_points])
    foreign = foreign_signs(g, known_text)
    if foreign:
        _, own = _signs(own_text)
        known = "、".join(f"`\\{c}`" for c in sorted(own & SYMBOL_SIGNS))
        add("symbol_foreign", f"講義的數學式用了課文沒有的符號 {'、'.join(f'`\\{c}`' for c in foreign)}；"
            + (f"這一段課文用的符號是 {known}，要改回課文的符號" if known else "課文沒有用這個符號，要改寫成不用它"),
            excerpt=_sentence_of(g, g.find("\\" + foreign[0])))
    if symbol_evidence:
        for d in notation_mismatches(symbol_evidence, [g]):
            add("notation_mismatch", d)
    if t.language == "src" and not CJK_SCRIPT_RE.search(t.text):
        # written in the source's own language, which uses none of these scripts: such characters are a slip into
        # another language mid-sentence (qwen models do this)
        for sentence in dict.fromkeys(_sentence_of(g, m.start()) for m in CJK_SCRIPT_RE.finditer(g)):
            runs = "、".join(dict.fromkeys(CJK_SCRIPT_RE.findall(sentence)))
            add("language_mixed", f"講義混入了課本沒有用到的文字「{runs}」，這一句要和課本用同一種語言", excerpt=sentence)
    return out


# -- what the handout tells the reader it could not settle ---------------------------------------------------------

NOTE_TEXT = {
    "point_missing": lambda f: f"這段講義可能沒有教到：{f.suggested_fix[:80]}",
    "conflict_unconfirmed": lambda f: f"這段講義的「{f.draft_excerpt[:60]}」可能和課本不一致，建議對照課本",
    "formula_missing": lambda f: f"這段講義沒有寫出課本的公式 {f.suggested_fix[:80]}",
    "notation_mismatch": lambda f: f.detail[:100],
    "symbol_foreign": lambda f: f.detail[:100],
    "math_word": lambda f: f.detail[:100],
    "language_mixed": lambda f: f"這段講義夾雜了其他語言的文字：{f.draft_excerpt[:80]}",
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
    if i != -1:
        return where[i], where[i + len(nq) - 1] + 1
    span = _near(flat, nq)
    return (where[span[0]], where[span[1] - 1] + 1) if span else None


NEAR_MIN_SHARE = 0.9   # a copied passage found only approximately: this share of it must be found word for word …
NEAR_MAX_SPREAD = 1.1  # … within a stretch at most this much longer than the passage
NEAR_MIN_RUN = 8       # shorter common runs are chance (single letters, spaces)


def _near(flat: str, q: str) -> tuple[int, int] | None:
    """Where a passage copied with small slips lies in `flat` (a model copying a long passage rewrites a symbol or a
    word on the way): the stretch holding nearly all of it in order."""
    if len(q) < 2 * NEAR_MIN_RUN:
        return None
    import difflib
    sm = difflib.SequenceMatcher(None, flat, q, autojunk=False)
    runs = [m for m in sm.get_matching_blocks() if m.size >= NEAR_MIN_RUN]
    if not runs:
        return None
    # the stretch around the longest run: the runs before / after it that keep the stretch compact
    best = max(runs, key=lambda m: m.size)
    keep = [m for m in runs if abs((m.a - m.b) - (best.a - best.b)) <= (NEAR_MAX_SPREAD - 1) * len(q) + NEAR_MIN_RUN]
    # widened by what the passage has before its first / after its last common run (a slip at either end)
    a = max(0, keep[0].a - keep[0].b)
    b = min(len(flat), keep[-1].a + keep[-1].size + len(q) - (keep[-1].b + keep[-1].size))
    if sum(m.size for m in keep) >= NEAR_MIN_SHARE * len(q) and b - a <= NEAR_MAX_SPREAD * len(q):
        return a, b
    return None



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
            if PARAGRAPH_BREAK_RE.search(text[a:b]):
                # a correction changes the sentences a problem is about; a passage running over paragraphs is the
                # handout written anew (a small model has copied most of it and replaced it with the textbook)
                skipped.append(f"{e.problem or '?'}：要換掉的文字跨了好幾段，修正只換有問題的那一兩句")
                continue
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
