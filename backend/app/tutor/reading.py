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


LANGUAGE_FIELDS = {
    "zh-TW": {"language": "繁體中文", "language_rule": "一律使用繁體中文（台灣用語）。"},
    "source": {"language": "原文的語言",
               "language_rule": "一律使用原文的語言撰寫：原文是英文就用英文，原文是中文就用繁體中文（台灣用語），其他語言亦同；"
                                "不要翻譯成其他語言。"},
}


def with_language(prompts: dict[str, str], language: str = "zh-TW") -> dict[str, str]:
    """The prompts with the language fields ({{language}}, {{language_rule}}) filled for a session's guide language.
    Prompts from before these fields have nothing to fill and stay as they were."""
    fields = LANGUAGE_FIELDS.get(language, LANGUAGE_FIELDS["zh-TW"])
    out = {}
    for k, text in prompts.items():
        for f, v in fields.items():
            text = text.replace("{{" + f + "}}", v)
        out[k] = text
    return out


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
