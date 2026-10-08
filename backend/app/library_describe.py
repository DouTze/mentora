"""A short description and an alias for each book in the library, written by the connected model from the original
file name, where the file came from and a little of its text (app.library records them).

Only a real model writes them: with the simulated backend nothing is written and the books wait. Books are described
right after their import, and at start any book still without a description is described in the background; a model
that does not answer stops the round (the next start tries again)."""
from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from . import config
from .llm.runtime import BackendPool, ModelRuntime, ModelSnapshot
from .llm.tasks import parse_model_json
from .materials import MaterialStore
from .reading.prompts import render

log = logging.getLogger("mentor.library")
EXCERPT_CHARS = 1200
CALL_TIMEOUT_S = 90


class BookNote(BaseModel):
    description: str = Field(description="一句話說明這是什麼（繁體中文，最多 60 字）")
    alias: str = Field(description="簡短好記的別名（2–16 字）")


class LibraryDescriber:
    def __init__(self, registry, pool: BackendPool, materials: MaterialStore):
        self.registry = registry
        self.pool = pool
        self.materials = materials
        self._tasks: set[asyncio.Task] = set()
        self._sweeping: asyncio.Task | None = None

    def available(self) -> bool:
        return not self.registry.backend.simulated

    def schedule(self, mid: str) -> None:
        """Describe one book in the background (right after its import)."""
        if not self.available():
            return
        t = asyncio.create_task(self._quiet(mid))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def sweep(self) -> None:
        """Describe, one after another in the background, every book that has no description yet."""
        if not self.available() or (self._sweeping and not self._sweeping.done()):
            return
        self._sweeping = asyncio.create_task(self._sweep())

    async def stop(self) -> None:
        tasks = [t for t in [*self._tasks, self._sweeping] if t and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sweep(self) -> None:
        for mid in self.materials.library.undescribed():
            if not self.available() or not await self._quiet(mid):
                return

    async def _quiet(self, mid: str) -> bool:
        try:
            await self.describe(mid)
            return True
        except Exception:  # noqa: BLE001 — a model that does not answer: the book waits for the next round
            log.warning("could not describe %s", mid, exc_info=True)
            return False

    async def _ask(self, prompt: str) -> BookNote:
        snap = ModelSnapshot.from_config(self.registry.cfg)
        self.pool.seed(snap, self.registry.backend)
        chat = ModelRuntime(snap, self.pool).chat_model(min_max_tokens=400)
        bound = chat.bind(json_schema=BookNote.model_json_schema(), meta={"task": "library_describe"})
        msg = await asyncio.wait_for(bound.ainvoke([HumanMessage(prompt)]), CALL_TIMEOUT_S)
        return parse_model_json(str(msg.content), BookNote)

    async def describe(self, mid: str) -> dict | None:
        """Write the book's description and alias now. None when the book or its record is gone."""
        try:
            m = self.materials.get(mid)
        except Exception:  # noqa: BLE001 — deleted meanwhile
            return None
        rec = m.get("library")
        if not rec:
            return None
        excerpt, n = [], 0
        for u in m["units"]:
            u = u.strip()
            if u:
                excerpt.append(u)
                n += len(u)
            if n >= EXCERPT_CHARS:
                break
        template = (config.PROMPT_DIR / "library_describe.md").read_text(encoding="utf-8")
        prompt = render(template, {"original_name": rec["original_name"], "original_path": rec["original_path"] or "（不知道）",
                                   "title": m["title"], "excerpt": "\n".join(excerpt)[:EXCERPT_CHARS] or "（沒有文字）"})
        note = await self._ask(prompt)
        alias, description = note.alias.strip()[:24], note.description.strip()[:120]
        if not alias or not description:
            return None
        who = self.registry.backend.describe()
        self.materials.library.describe(mid, alias, description, f"{who.get('backend')}:{who.get('model')}")
        return {"alias": alias, "description": description}
