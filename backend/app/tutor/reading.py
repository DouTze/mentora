"""Helpers shared by the reading sessions (app/workflows): the session's initial reading state, the language the
model writes in, excerpts for labels, and the Q&A history given to the tutor when the reader asks."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

ASK_HISTORY_LIMIT = 8   # exchanges from earlier quotes kept in the prompt (the current quote's are always all kept)


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


def initial_state(content: dict) -> dict[str, Any]:
    """Session state while reading. Question fields exist (empty) so the UI/engine shapes stay uniform."""
    return {
        "phase": "reading", "reading": {"index": 0, "done": False, "guides": {}, "qa": {}, "compacts": {}},
        "concept_order": [], "concept_index": 0, "question_id": None, "hint_count": 0, "probe_count": 0,
        "concepts": {}, "stuck_at": [],
    }


class Answer(BaseModel):
    answer: str = Field(min_length=1)


def guide_language(material: dict) -> str:
    """What the model writes in for a session on this material: "zh-TW" when the material asks for its guides in
    繁體中文, else "source" (the material's own language). Sessions from before this setting were all "zh-TW"."""
    return "zh-TW" if material.get("translate_zh") else "source"


ZH_RULE = "一律使用繁體中文（台灣用語）。"
SCRIPTS = (("日文", re.compile(r"[぀-ヿ]")), ("韓文", re.compile(r"[가-힯ᄀ-ᇿ]")),
           ("中文", re.compile(r"[㐀-䶿一-鿿豈-﫿]")), ("拉丁", re.compile(r"[A-Za-zÀ-ɏ]")))
# the most frequent English function words: in English prose they are well over a tenth of the words
EN_FUNCTION_WORDS = frozenset("the of and to a in is that for it as with are be by this on not or from an which "
                              "we can at have its these their was were has but if each".split())


def source_language(text: str) -> str | None:
    """The language of a passage, named for the prompts, from its writing system (kana → 日文, hangul → 韓文, han
    → 中文; Latin letters are 英文 when English function words are a tenth of its words). None when it cannot be
    told: the prompt then asks for "the source's language" in general."""
    text = (text or "")[:20000]   # the opening of a range says what it is written in
    counts = {name: len(rx.findall(text)) for name, rx in SCRIPTS}
    total = sum(counts.values())
    if not total:
        return None
    if counts["日文"] >= 0.1 * total:
        return "日文"
    if counts["韓文"] >= 0.3 * total:
        return "韓文"
    if counts["中文"] >= 0.3 * total:
        return "中文"
    words = re.findall(r"[A-Za-z]+", text)
    if counts["拉丁"] >= 0.5 * total and words and sum(w.lower() in EN_FUNCTION_WORDS for w in words) >= 0.1 * len(words):
        return "英文"
    return None


ZH_OUTPUT = {"code": "zh", "name": "繁體中文"}


def output_language(guide_lang: str = "zh-TW", material_language: str | None = None) -> dict:
    """What a session writes in, decided when it starts: {"code": "zh" | "src", "name": the language, or None when
    it cannot be told}. 繁體中文 when the reader asked for it or the material is Chinese; otherwise the material's
    own language (read when it was imported)."""
    if guide_lang == "zh-TW" or material_language == "中文":
        return dict(ZH_OUTPUT)
    return {"code": "src", "name": material_language}


def session_output(meta: dict, source_text: str = "") -> dict:
    """A session's output language: stored when it started; for a session from before that, read from its source."""
    if meta.get("output_language"):
        return meta["output_language"]
    return output_language(meta.get("guide_language", "zh-TW"), source_language(source_text))


def language_fields(out: dict | None = None) -> dict[str, str]:
    """{{language}} and {{language_rule}}: the output language named outright. A small model asked to "write in the
    source's language" among Chinese instructions writes Chinese; told "英文", it writes English."""
    out = out or ZH_OUTPUT
    if out.get("code") != "src":
        return {"language": "繁體中文", "language_rule": ZH_RULE}
    if out.get("name"):
        return {"language": out["name"], "language_rule": f"一律使用{out['name']}撰寫，和課本原文相同的語言。"}
    return {"language": "課本原文的語言", "language_rule": "一律使用和課本原文相同的語言撰寫，不要翻譯成其他語言。"}


def with_language(prompts: dict[str, str], out: dict | None = None) -> dict[str, str]:
    """The prompts with the language fields ({{language}}, {{language_rule}}) filled for a session's output
    language. Prompts from before these fields have nothing to fill and stay as they were."""
    fields = language_fields(out)
    filled = {}
    for k, text in prompts.items():
        for f, v in fields.items():
            text = text.replace("{{" + f + "}}", v)
        filled[k] = text
    return filled


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
