"""Prompt bundle of the LangGraph workflow. A new session stores the full text of every prompt it may use (not
only a hash), so an old session keeps running — and can be reproduced — with exactly the prompts it started with."""
from __future__ import annotations

from .. import config
from ..llm.runtime import stable_hash

BUNDLE_VERSION = "reading-bundle-v4"
PROMPT_IDS = ("segment_blocks", "segment_split", "segment_topic_summary", "segment_outline", "segment_matter", "segment_boundaries", "segment_passages", "segment_topic", "reading_topic",
              "reading_symbols", "reading_intent", "reading_body", "reading_review_points", "reading_review_conflicts", "reading_fix",
              "reading_notes", "reading_notes_revise", "reading_notes_audit", "quiz_generate",
              "quiz_audit", "ask", "compact", "generate", "assess")


def load_bundle() -> dict:
    texts = {}
    for pid in PROMPT_IDS:
        p = config.PROMPT_DIR / f"{pid}.md"
        if p.exists():
            texts[pid] = p.read_text(encoding="utf-8")
    return {"version": BUNDLE_VERSION, "hash": stable_hash(texts), "prompts": texts}


def render(template: str, fields: dict[str, str]) -> str:
    out = template
    for k, v in fields.items():
        out = out.replace("{{" + k + "}}", v)
    return out
