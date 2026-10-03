"""Prompt bundle of the LangGraph workflow. A new session stores the full text of every prompt it may use (not
only a hash), so an old session keeps running — and can be reproduced — with exactly the prompts it started with."""
from __future__ import annotations

from .. import config
from ..llm.runtime import stable_hash

BUNDLE_VERSION = "reading-bundle-v1"
PROMPT_IDS = ("segment_blocks", "segment_seam", "reading_analyze", "reading_context", "reading_draft",
              "reading_review", "reading_verify", "reading_revise", "ask", "compact", "generate", "assess")


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
