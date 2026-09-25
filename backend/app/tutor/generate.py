"""Turns a user-selected range of an imported material into a content dict
(same shape as app/content/*.json) so the rule engine can run on it unchanged."""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from ..db import new_id
from . import library

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
MIN_CONCEPTS, MAX_CONCEPTS = 1, 3


class InvalidGeneration(Exception):
    pass


class GenQuestion(BaseModel):
    prompt: str = Field(min_length=4)
    rubric: str = Field(min_length=4)
    hints: list[str] = Field(default_factory=list)


class GenConcept(BaseModel):
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=4)
    examples: list[str] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    diagnostic: GenQuestion
    transfer: GenQuestion


class Generated(BaseModel):
    title: str = Field(min_length=1)
    concepts: list[GenConcept] = Field(min_length=1)


def range_label(unit: str, start: int, end: int) -> str:
    name = "頁" if unit == "page" else "行"
    return f"第 {start} {name}" if start == end else f"第 {start}–{end} {name}"


def build_messages(material: dict, start: int, end: int, text: str, *, from_notes: bool = False,
                   reading_log: str = "") -> list[dict[str, str]]:
    """`text` is the range itself, or (from_notes) the compact notes written while reading a long range.
    `reading_log` is the 導讀 record (key points + the student's questions), which steers the questions."""
    prompt = library.render_prompt(library.load_prompt("generate"), {
        "material_title": material["title"], "range_label": range_label(material["unit"], start, end),
        "range_text": text, "min_concepts": str(MIN_CONCEPTS), "max_concepts": str(MAX_CONCEPTS),
        "source_kind": "精簡筆記（原文已在導讀時讀過，這裡只保留出題所需的重點）" if from_notes else "教材原文",
        "reading_log": reading_log or "（沒有導讀紀錄）",
    })
    return [{"role": "user", "content": prompt}]


def parse(text: str) -> Generated:
    cleaned = THINK_RE.sub("", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        raise InvalidGeneration("模型輸出不含 JSON 物件")
    try:
        return Generated.model_validate(json.loads(m.group(0)))
    except ValueError as e:  # json errors and pydantic ValidationError both derive from ValueError
        detail = f"{e.errors()[0]['msg']} @ {e.errors()[0]['loc']}" if isinstance(e, ValidationError) else str(e)
        raise InvalidGeneration(f"出題結果格式不符：{detail}") from e


def empty_content(material: dict, start: int, end: int, text: str) -> dict:
    """Content for a session that is still in 導讀: the source is known, the concepts/questions come later."""
    label = range_label(material["unit"], start, end)
    return {
        "id": f"gen-{new_id()}", "version": "generated", "title": f"{material['title']} {label}",
        "description": f"由「{material['title']}」{label}產生",
        "source": {"material_id": material["id"], "material_title": material["title"], "unit": material["unit"],
                   "start": start, "end": end, "label": label, "text": text},
        "quotes": [], "parts": [], "concepts": [], "questions": [],
    }


def fill_content(content: dict, gen: Generated) -> dict:
    """Add the generated concepts/questions to an empty_content() dict (in place, returned for convenience)."""
    filled = to_content(gen, {"id": content["source"]["material_id"], "title": content["source"]["material_title"],
                              "unit": content["source"]["unit"]}, content["source"]["start"], content["source"]["end"],
                        content["source"]["text"])
    content.update({"title": filled["title"], "concepts": filled["concepts"], "questions": filled["questions"]})
    return content


def to_content(gen: Generated, material: dict, start: int, end: int, text: str) -> dict:
    concepts, questions = [], []
    for i, c in enumerate(gen.concepts[:MAX_CONCEPTS], 1):
        cid = f"concept-{i:02d}"
        concepts.append({"id": cid, "title": c.title, "explanation": c.explanation,
                         "examples": c.examples, "key_terms": c.key_terms})
        for qtype, q in (("diagnostic", c.diagnostic), ("transfer", c.transfer)):
            questions.append({"id": f"q-{'diag' if qtype == 'diagnostic' else 'transfer'}-{i:02d}", "concept_id": cid,
                              "type": qtype, "prompt": q.prompt, "rubric": q.rubric, "hints": q.hints})
    label = range_label(material["unit"], start, end)
    return {
        "id": f"gen-{new_id()}", "version": "generated", "title": gen.title,
        "description": f"由「{material['title']}」{label}產生",
        "source": {"material_id": material["id"], "material_title": material["title"], "unit": material["unit"],
                   "start": start, "end": end, "label": label, "text": text},
        "concepts": concepts, "questions": questions,
    }
