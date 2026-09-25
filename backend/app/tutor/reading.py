"""導讀（guided reading）before the quiz.

The chosen range is cut into *quotes* — the 來源單元 of WaAi-ML's reading guides: one complete
paragraph-level thought each, never a fixed number of characters. Each quote is taught on its own
(中文課文 + 主要正文) and the student moves on only when they say they understood. Quotes are also
grouped into *parts* that fit a small model's context; each part is compacted after it has been read,
and the quiz is written from those compact notes when the whole range is too long to feed at once.

Everything here is pure: splitting, part planning, prompt building and output parsing.
Model calls and persistence live in reading_agent.py / session.py."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from . import library

# Granularity. The ideal quote is one paragraph-level thought — in WaAi-ML's guides a quote is often a single
# defining sentence or one short paragraph, never a page. So: one original paragraph = one quote; only a tiny
# transition line (< MIN) joins its neighbour; a paragraph longer than MAX (PDF text usually has no paragraph
# breaks) is cut at sentence ends into pieces of about TARGET characters.
QUOTE_MIN_CHARS = 40
QUOTE_TARGET_CHARS = 350
QUOTE_MAX_CHARS = 700
# A part is what the model reads in one go when compacting; sized for a 4–8B model with 16k context.
PART_CHARS = 5000
# Below this the quiz is written from the original text; above it, from the compact notes of each part.
GEN_TEXT_BUDGET = 12000

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
SENTENCE_END_RE = re.compile(r"(?<=[。！？!?])\s*|(?<=[.;])\s+(?=[A-Z一-鿿])")


class InvalidReadingOutput(Exception):
    pass


# -- splitting ---------------------------------------------------------------

def _paragraphs(units: list[str], unit: str, start: int) -> list[dict]:
    """Paragraphs with their location. unit='line': blank lines separate, `#` lines are headings.
    unit='page': blank lines inside a page separate; a page without any is one paragraph."""
    out: list[dict] = []
    if unit == "line":
        buf: list[str] = []
        first = 0
        for i, raw in enumerate(units):
            n = start + i
            line = raw.rstrip()
            if not line.strip():
                if buf:
                    out.append({"text": "\n".join(buf), "loc": (first, n - 1), "heading": False})
                    buf = []
                continue
            if HEADING_RE.match(line):
                if buf:
                    out.append({"text": "\n".join(buf), "loc": (first, n - 1), "heading": False})
                    buf = []
                out.append({"text": line.strip().lstrip("#").strip(), "loc": (n, n), "heading": True})
                continue
            if not buf:
                first = n
            buf.append(line)
        if buf:
            out.append({"text": "\n".join(buf), "loc": (first, start + len(units) - 1), "heading": False})
        return out
    for i, page in enumerate(units):
        n = start + i
        paras = [p.strip() for p in re.split(r"\n\s*\n", page) if p.strip()]
        for k, p in enumerate(paras, 1):
            lines = p.split("\n")
            is_heading = len(lines) == 1 and len(p) <= 40 and not re.search(r"[。．.!?！？,，;；]$", p) and len(paras) > 1
            out.append({"text": p, "loc": (n, n), "heading": is_heading, "para": k, "paras": len(paras)})
    return out


def _cut_long(text: str, target: int) -> list[str]:
    """Cut an oversize paragraph at sentence ends into pieces of about `target` chars (never mid-sentence;
    a single sentence longer than target stands alone)."""
    pieces, buf = [], ""
    for s in (x for x in SENTENCE_END_RE.split(text) if x and x.strip()):
        if buf and len(buf) + len(s) > target:
            pieces.append(buf.strip())
            buf = s
        else:
            buf += s
    if buf.strip():
        pieces.append(buf.strip())
    return pieces or [text]


def _label(unit: str, loc: tuple[int, int], paras: list[dict]) -> str:
    if unit == "page":
        if loc[0] != loc[1]:
            return f"第 {loc[0]}–{loc[1]} 頁"
        base = f"第 {loc[0]} 頁"
        if paras[0].get("paras", 1) <= 1:
            return base
        a, b = paras[0]["para"], paras[-1]["para"]
        return f"{base} 第 {a} 段" if a == b else f"{base} 第 {a}–{b} 段"
    return f"第 {loc[0]} 行" if loc[0] == loc[1] else f"第 {loc[0]}–{loc[1]} 行"


def _excerpt(text: str, head: int = 24, tail: int = 16) -> str:
    """"opening words … closing words". A cut never lands inside a $…$ formula (it would break rendering)."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= head + tail + 3:
        return flat
    h = head
    if flat[:h].count("$") % 2:          # inside a formula → extend to its closing $
        end = flat.find("$", h)
        h = end + 1 if end != -1 else h
    t = len(flat) - tail
    if flat[t:].count("$") % 2:
        start = flat.rfind("$", 0, t)
        t = start if start != -1 else t
    if h >= t:
        return flat
    return f"{flat[:h].rstrip()} … {flat[t:].lstrip()}"


def split_atoms(units: list[str], unit: str, start: int, *,
                min_chars: int | None = None, max_chars: int | None = None, target_chars: int | None = None) -> list[dict]:
    """Rule-based candidate units ("atoms"): one original paragraph each, a tiny transition line joined to its
    neighbour, a break-less oversize paragraph cut at sentence ends. The model then groups atoms into the final
    quotes (segment_messages / apply_segmentation); without a model the atoms are the quotes.
    Each atom: {"heading", "paras", "text", "chars"}. units[0] is unit number `start`."""
    min_chars, max_chars = min_chars or QUOTE_MIN_CHARS, max_chars or QUOTE_MAX_CHARS
    target_chars = target_chars or QUOTE_TARGET_CHARS
    groups: list[dict] = []
    pending_heading: str | None = None

    def chars(g: dict) -> int:
        return sum(len(p["text"]) for p in g["paras"])

    for p in _paragraphs(units, unit, start):
        if p["heading"]:
            pending_heading = (pending_heading + " › " + p["text"]) if pending_heading else p["text"]
            continue
        if len(p["text"]) > max_chars:
            for j, piece in enumerate(_cut_long(p["text"], target_chars)):
                groups.append({"heading": pending_heading if j == 0 else None, "paras": [{**p, "text": piece, "cut": j + 1}]})
                pending_heading = None
            continue
        cur = groups[-1] if groups else None
        fits = cur is not None and chars(cur) + len(p["text"]) <= max_chars
        short = cur is not None and (chars(cur) < min_chars or len(p["text"]) < min_chars)
        if cur is not None and pending_heading is None and fits and short:
            cur["paras"].append(p)
        else:
            groups.append({"heading": pending_heading, "paras": [p]})
            pending_heading = None
    if pending_heading and groups:  # trailing heading with nothing under it: keep it visible on the last quote
        groups[-1]["paras"].append({"text": pending_heading, "loc": groups[-1]["paras"][-1]["loc"], "heading": False})
    for g in groups:
        g["text"] = "\n\n".join(p["text"] for p in g["paras"])
        g["chars"] = len(g["text"])
    return groups


def finish_quotes(unit: str, groups: list[dict]) -> list[dict]:
    """Atoms / merged atoms → quote dicts with ids, labels and excerpts."""
    quotes: list[dict] = []
    for i, g in enumerate(groups):
        ps = g["paras"]
        loc = (ps[0]["loc"][0], ps[-1]["loc"][1])
        text = "\n\n".join(p["text"] for p in ps)
        label = _label(unit, loc, ps)
        if len(ps) == 1 and ps[0].get("cut"):
            label += f"（之 {ps[0]['cut']}）"
        elif ps[0].get("cut") and ps[-1].get("cut") and ps[0]["loc"] == ps[-1]["loc"]:
            label += f"（之 {ps[0]['cut']}–{ps[-1]['cut']}）"
        quotes.append({"id": f"quote-{i + 1:02d}", "index": i, "label": label, "heading": g.get("heading"),
                       "excerpt": _excerpt(text), "text": text, "chars": len(text), "part": 0})
    return quotes


def split_quotes(units: list[str], unit: str, start: int, **kw) -> list[dict]:
    """Rule-based quotes (no model): the atoms themselves."""
    return finish_quotes(unit, split_atoms(units, unit, start, **kw))


# -- model segmentation (the WaAi-ML 來源單元 rules, decided by the model over the candidate atoms) -------

class SegUnit(BaseModel):
    atoms: list[int] = Field(min_length=1)   # 1-based, contiguous, ascending
    title: str | None = None


class Segmentation(BaseModel):
    units: list[SegUnit] = Field(min_length=1)


def plan_segments(groups: list[dict], chunk_chars: int | None = None) -> list[tuple[int, int]]:
    """Contiguous [a, b) atom ranges small enough to segment in one model call."""
    chunk_chars = chunk_chars or PART_CHARS
    out: list[tuple[int, int]] = []
    a, size = 0, 0
    for i, g in enumerate(groups):
        if i > a and size + g["chars"] > chunk_chars:
            out.append((a, i))
            a, size = i, 0
        size += g["chars"]
    if a < len(groups):
        out.append((a, len(groups)))
    return out


def segment_messages(material_title: str, groups: list[dict]) -> list[dict]:
    listing = "\n\n".join(f"【{i + 1}】" + (f"（小標題：{g['heading']}）\n" if g.get("heading") else "") + g["text"]
                           for i, g in enumerate(groups))
    prompt = library.render_prompt(library.load_prompt("segment"), {
        "material_title": material_title, "count": str(len(groups)), "atoms": listing,
    })
    return [{"role": "user", "content": prompt}]


def apply_segmentation(groups: list[dict], seg: Segmentation) -> list[dict]:
    """Merge atoms as the model decided. Every atom must be used exactly once, in order, contiguously;
    anything else is an invalid output (the caller falls back to the atoms)."""
    expect = 1
    merged: list[dict] = []
    for u in seg.units:
        ids = u.atoms
        if ids != list(range(ids[0], ids[0] + len(ids))) or ids[0] != expect:
            raise InvalidReadingOutput(f"單元 {ids} 不連續或跳過了第 {expect} 個候選段落")
        expect = ids[-1] + 1
        if expect - 1 > len(groups):
            raise InvalidReadingOutput(f"單元 {ids} 超出候選段落數 {len(groups)}")
        parts = groups[ids[0] - 1:ids[-1]]
        heading = next((g["heading"] for g in parts if g.get("heading")), None)
        paras = [p for g in parts for p in g["paras"]]
        text = "\n\n".join(g["text"] for g in parts)
        merged.append({"heading": heading, "paras": paras, "text": text, "chars": len(text)})
    if expect - 1 != len(groups):
        raise InvalidReadingOutput(f"只分配了 {expect - 1}/{len(groups)} 個候選段落")
    return merged


def plan_parts(quotes: list[dict], part_chars: int | None = None) -> list[dict]:
    """Contiguous groups of quotes, each ≤ part_chars (a single oversize quote is its own part).
    Sets quote['part'] in place."""
    part_chars = part_chars or PART_CHARS
    parts: list[dict] = []
    cur: dict | None = None
    for q in quotes:
        if cur is None or cur["chars"] + q["chars"] > part_chars:
            cur = {"index": len(parts), "quote_from": q["index"], "quote_to": q["index"], "chars": 0}
            parts.append(cur)
        cur["quote_to"] = q["index"]
        cur["chars"] += q["chars"]
        q["part"] = cur["index"]
    for p in parts:
        a, b = quotes[p["quote_from"]]["label"], quotes[p["quote_to"]]["label"]
        p["label"] = a if a == b else f"{a} 至 {b}"
    return parts


def initial_state(content: dict) -> dict[str, Any]:
    """Session state while reading. Question fields exist (empty) so the UI/engine shapes stay uniform."""
    return {
        "phase": "reading", "reading": {"index": 0, "done": False, "guides": {}, "qa": {}, "compacts": {}},
        "concept_order": [], "concept_index": 0, "question_id": None, "hint_count": 0, "probe_count": 0,
        "concepts": {}, "stuck_at": [],
    }


# -- model I/O ---------------------------------------------------------------

class Guide(BaseModel):
    chinese_text: str | None = None          # 中文課文：faithful rendering; null when the source is already Chinese
    guide: str = Field(min_length=10)        # 主要正文：self-contained teaching text
    key_points: list[str] = Field(default_factory=list)
    check: str = ""                          # one question the student can use to check themselves


class Answer(BaseModel):
    answer: str = Field(min_length=1)


class Compact(BaseModel):
    summary: str = Field(min_length=10)
    key_terms: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)


def parse_json(text: str, model: type[BaseModel]) -> BaseModel:
    cleaned = THINK_RE.sub("", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        raise InvalidReadingOutput("模型輸出不含 JSON 物件")
    try:
        return model.model_validate(json.loads(m.group(0)))
    except ValueError as e:
        detail = f"{e.errors()[0]['msg']} @ {e.errors()[0]['loc']}" if isinstance(e, ValidationError) else str(e)
        raise InvalidReadingOutput(f"導讀結果格式不符：{detail}") from e


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def is_chinese(text: str) -> bool:
    """True when the text is (mostly) written in Chinese; then no 中文課文 is needed."""
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and len(CJK_RE.findall(text)) / len(letters) >= 0.3


def chinese_text_rule(quote: dict) -> str:
    if is_chinese(quote["text"]):
        return "這一段原文已經是中文：chinese_text 填 null。"
    return "這一段原文不是中文：chinese_text **必填**，不得為 null 或空字串。"


def check_guide(quote: dict, guide: "Guide") -> None:
    """Rules a small model tends to break; violations count as invalid output (the caller retries once)."""
    if not is_chinese(quote["text"]) and not (guide.chinese_text or "").strip():
        raise InvalidReadingOutput("原文不是中文，但沒有產生中文課文（chinese_text）")


def guide_messages(material_title: str, range_label: str, quote: dict, total: int, prev_points: list[str]) -> list[dict]:
    prompt = library.render_prompt(library.load_prompt("guide"), {
        "material_title": material_title, "range_label": range_label,
        "position": f"{quote['index'] + 1}/{total}", "quote_label": quote["label"],
        "heading": quote.get("heading") or "（無）", "quote_text": quote["text"],
        "prev_points": "\n".join(f"- {p}" for p in prev_points) or "（這是第一段）",
        "chinese_text_rule": chinese_text_rule(quote),
    })
    return [{"role": "user", "content": prompt}]


ASK_HISTORY_LIMIT = 8   # exchanges from earlier quotes kept in the prompt (the current quote's are always all kept)


def ask_history(quotes: list[dict], rd: dict, index: int) -> list[dict]:
    """Q&A so far, oldest first: everything asked on the current quote plus the most recent exchanges on
    earlier quotes, each tagged with its quote so the tutor can refer back ("你剛才在第 2 段問過…")."""
    earlier = [{"quote_index": q["index"], "quote_label": q["label"], **x}
               for q in quotes[:index] for x in rd["qa"].get(str(q["index"]), [])]
    current = [{"quote_index": index, "quote_label": quotes[index]["label"], **x} for x in rd["qa"].get(str(index), [])]
    return earlier[-ASK_HISTORY_LIMIT:] + current


def format_history(history: list[dict], current_index: int) -> str:
    lines = []
    for h in history:
        where = "這一段" if h.get("quote_index", current_index) == current_index else f"第 {h['quote_index'] + 1} 段（{h.get('quote_label', '')}）"
        lines.append(f"［{where}］學生：{h['question']}\n　導師：{h['answer']}")
    return "\n".join(lines) or "（無）"


def ask_messages(material_title: str, quote: dict, guide: dict | None, history: list[dict], question: str) -> list[dict]:
    prompt = library.render_prompt(library.load_prompt("ask"), {
        "material_title": material_title, "quote_label": quote["label"], "quote_text": quote["text"],
        "guide": (guide or {}).get("guide") or "（尚未產生）",
        "history": format_history(history, quote["index"]),
        "question": question,
    })
    return [{"role": "user", "content": prompt}]


def compact_messages(material_title: str, part: dict, quotes: list[dict]) -> list[dict]:
    body = "\n\n".join(f"[{q['label']}]\n{q['text']}" for q in quotes)
    prompt = library.render_prompt(library.load_prompt("compact"), {
        "material_title": material_title, "part_label": part["label"], "part_text": body,
    })
    return [{"role": "user", "content": prompt}]


def compact_markdown(part: dict, c: Compact) -> str:
    lines = [f"# 精簡筆記：{part['label']}", "", c.summary, ""]
    if c.claims:
        lines += ["## 主要論點"] + [f"- {x}" for x in c.claims] + [""]
    if c.key_terms:
        lines += ["## 關鍵詞", "、".join(c.key_terms), ""]
    return "\n".join(lines)


def quiz_source_text(full_text: str, parts: list[dict], compacts: dict[str, dict]) -> tuple[str, str]:
    """(text for the question writer, mode). Full text when it fits, else the compact notes in order."""
    if len(full_text) <= GEN_TEXT_BUDGET or not compacts:
        return full_text, "full"
    notes = []
    for p in parts:
        c = compacts.get(str(p["index"]))
        if c:
            notes.append(c["markdown"])
    return "\n\n".join(notes), "compact"


def reading_log(quotes: list[dict], rd: dict) -> str:
    """The 導讀 record as text for the question writer: per quote, the points the guide established and every
    question the student asked (with the tutor's answer). Questions mark what the student found hard, so the
    quiz should return to those spots."""
    lines: list[str] = []
    for q in quotes:
        g = rd["guides"].get(str(q["index"]))
        qa = rd["qa"].get(str(q["index"]), [])
        if not g and not qa:
            continue
        lines.append(f"### 第 {q['index'] + 1} 段（{q['label']}" + (f"，{q['heading']}" if q.get("heading") else "") + "）")
        for pt in (g or {}).get("key_points", []):
            lines.append(f"- 重點：{pt}")
        for x in qa:
            lines.append(f"- 學生問：{x['question']}")
            lines.append(f"  導師答：{x['answer']}")
    return "\n".join(lines) or "（學生在導讀時沒有提問）"
