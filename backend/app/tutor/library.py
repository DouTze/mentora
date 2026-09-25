"""Loads content (教材) and strategy (教學規則) files. Both are plain JSON so they can be swapped."""
from __future__ import annotations

import json
from pathlib import Path

from .. import config


def _load_dir(d: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted(d.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        out[data["id"]] = data
    return out


def load_contents() -> dict[str, dict]:
    return _load_dir(config.CONTENT_DIR)


def load_strategies() -> dict[str, dict]:
    return _load_dir(config.STRATEGY_DIR)


def load_prompt(prompt_id: str) -> str:
    return (config.PROMPT_DIR / f"{prompt_id}.md").read_text(encoding="utf-8")


def render_prompt(template: str, fields: dict[str, str]) -> str:
    out = template
    for k, v in fields.items():
        out = out.replace("{{" + k + "}}", v)
    return out
