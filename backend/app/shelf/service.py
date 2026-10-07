"""What the API needs from the shelf: an instant view, background scout runs, the person's saved reading wishes and
importing chosen books (optionally gathered into one category, the 閱讀區).

Nothing here waits for the model. `view()` always answers from the last finished run (state.json) or, before the
first run, from the keyword ranking; a run started by `start()` replaces it when it finishes, and the latest
request wins (a new query cancels the run still going)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ..db import Database
from ..llm.registry import Registry
from ..llm.runtime import BackendPool, ModelRuntime, ModelSnapshot
from ..materials import MaterialError, MaterialStore
from .books import BookMeta, keywords_of, rank
from .index import ShelfIndex, series_files
from .scout import SHELF_SIZE, ShelfScout, log_line

log = logging.getLogger("mentor.shelf")
INTERESTS_KEY = "shelf_interests"
MAX_INTERESTS = 12
STALE_S = 6 * 3600


class ShelfService:
    def __init__(self, db: Database, registry: Registry, pool: BackendPool, materials: MaterialStore,
                 directory: Path, roots: list[Path] | None = None):
        self.db, self.registry, self.pool, self.materials = db, registry, pool, materials
        self.index = ShelfIndex(directory, roots)
        self.meta = BookMeta(self.index)
        self.state_file = directory / "state.json"
        self.result = self._load_result()
        self.task: asyncio.Task | None = None

    # -- state ----------------------------------------------------------------
    def _load_result(self) -> dict | None:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_result(self, result: dict) -> None:
        self.result = result
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    @property
    def preparing(self) -> bool:
        return self.task is not None and not self.task.done()

    def interests(self) -> list[str]:
        return list(self.db.get_setting(INTERESTS_KEY) or [])

    def remember(self, text: str) -> None:
        text = " ".join(text.split())[:60]
        if text:
            self.db.set_setting(INTERESTS_KEY, [text] + [x for x in self.interests() if x != text][:MAX_INTERESTS - 1])

    def _exclude(self) -> set[str]:
        """Books already in the library stay off the shelf (matched by file name and by title)."""
        return {s.lower() for m in self.materials.list() for s in (m["filename"], m["title"])}

    def view(self) -> dict:
        exclude = self._exclude()
        r = self.result
        if r:
            books = [b for b in r["books"] if b["title"].lower() not in exclude and self.index.get(b["id"])]
        else:
            books = [self.meta.book(e, reason="", tendency=None) for e, _ in rank(self.index.entries, [], exclude=exclude, limit=SHELF_SIZE, known=self.meta.known())]
        return {"status": "preparing" if self.preparing else "ready", "query": (r or {}).get("query"),
                "tendencies": (r or {}).get("tendencies", []), "books": books, "interests": self.interests(),
                "simulated": (r or {}).get("simulated", False), "indexed": len(self.index.entries)}

    def quick(self, q: str) -> list[dict]:
        """Keyword search over the index, no model: what the shelf shows while the person is still typing."""
        kws = keywords_of(q)
        if not kws:
            return []
        books = [self.meta.book(e, reason="", tendency=None) for e, _ in rank(self.index.entries, kws, exclude=self._exclude(), limit=SHELF_SIZE, known=self.meta.known())]
        self.meta.save()
        return books

    # -- scout runs -----------------------------------------------------------
    def start(self, query: str | None = None) -> None:
        if query:
            self.remember(query)
        if self.preparing:
            self.task.cancel()
        self.task = asyncio.create_task(self._run(query))

    async def warmup(self) -> None:
        """At app start: refresh a stale index, then prepare a shelf if there is none or it is old, so the home page
        opens with books already chosen."""
        try:
            if not self.index.fresh:
                await self._rescan()
            if not self.result or time.time() - self.result.get("at", 0) > STALE_S:
                self.start(self.result.get("query") if self.result else None)
        except Exception:
            log.exception("shelf warmup failed")

    async def _rescan(self) -> None:
        entries = await asyncio.to_thread(self.index.rescan)
        await asyncio.to_thread(self.meta.enrich, entries)

    async def _run(self, query: str | None) -> None:
        try:
            if not self.index.fresh or not self.index.entries:
                await self._rescan()
            snap = ModelSnapshot.from_config(self.registry.cfg)
            self.pool.seed(snap, self.registry.backend)    # one adapter (one OpenVINO pipeline) per physical model
            scout = ShelfScout(self.index, self.meta, ModelRuntime(snap, self.pool))
            sessions = self.db.list_sessions()[:10]
            out = await scout.run({
                "query": query, "interests": [x for x in self.interests() if x != query],
                "library": [m["title"] for m in self.materials.list()][-15:],
                "reading": list(dict.fromkeys((s["content_snapshot"].get("source") or {}).get("material_title") or s["content_snapshot"]["title"]
                                              for s in sessions)),
                "exclude": self._exclude(),
                "series": [(e.name, e.parts) for e in sorted(self.index.entries, key=lambda e: -e.parts) if e.parts >= 10],
            })
            log.info("shelf scout: %s", log_line(out))
            self._save_result({"at": time.time(), "query": query, "tendencies": [t["label"] for t in out.get("tendencies") or []],
                               "books": out.get("books") or [], "simulated": self.registry.backend.simulated})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("shelf scout failed")

    # -- files ----------------------------------------------------------------
    def cover(self, eid: str) -> Path | None:
        e = self.index.get(eid)
        return self.meta.cover(e) if e else None

    def import_books(self, ids: list[str], category_name: str | None) -> dict:
        entries = [self.index.get(i) for i in ids]
        if not entries or any(e is None for e in entries):
            raise MaterialError("not_found", "找不到這本書，書櫃可能已經更新。")
        category = None
        if category_name and category_name.strip():
            name = category_name.strip()[:40]
            category = next((c for c in self.materials.list_categories() if c["name"] == name), None) or self.materials.create_category(name)
        imported = []
        for e in entries:
            try:
                if e.parts:   # a series: its chapters in order, as one text
                    data = "\n\n".join(Path(p).read_text(encoding="utf-8", errors="replace").strip() for p in series_files(e.path)).encode("utf-8")
                    filename = f"{e.name}.{'md' if e.kind == 'md' else 'txt'}"
                else:
                    data, filename = Path(e.path).read_bytes(), Path(e.path).name
            except OSError as err:
                raise MaterialError("unreadable", f"讀不到「{e.name}」：{err.strerror or err}") from err
            m = self.materials.add(filename, data, category["id"] if category else None)
            # the title the shelf showed (PDF metadata or a cleaned file name), not the raw file name
            imported.append(self.materials.update(m["id"], title=self.meta.book(e)["title"], category_id=None, set_category=False))
        if self.result:   # what was imported leaves the shelf right away
            self._save_result({**self.result, "books": [b for b in self.result["books"] if b["id"] not in ids]})
        return {"category": category, "materials": imported}
