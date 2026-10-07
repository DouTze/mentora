"""What the API needs from the shelf: an instant view, background scout runs, the person's saved reading wishes and
importing chosen books (optionally gathered into one category, the 閱讀區).

The scope (which folders may be searched, which are kept out) is the person's choice, saved in the settings table;
changing it drops everything the shelf knew about files outside it — index entries, opening text, covers and books
on the shelf — before anything else is shown.

Nothing here waits for the model. `view()` always answers from the last finished run (state.json) or, before the
first run, from the keyword ranking; a run started by `start()` replaces it when it finishes, and the latest
request wins (a new query cancels the run still going)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from ..db import Database
from ..llm.registry import Registry
from ..llm.runtime import BackendPool, ModelRuntime, ModelSnapshot
from ..materials import MaterialError, MaterialStore
from .books import BookMeta, rank
from .index import ShelfIndex, candidate_roots, series_files
from .topics import expand
from .scout import SHELF_SIZE, ShelfScout, log_line

log = logging.getLogger("mentor.shelf")
INTERESTS_KEY = "shelf_interests"
SCOPE_KEY = "shelf_scope"
MAX_INTERESTS = 12
STALE_S = 6 * 3600


class ShelfService:
    def __init__(self, db: Database, registry: Registry, pool: BackendPool, materials: MaterialStore,
                 directory: Path, roots: list[Path] | None = None):
        """`roots`: the folders offered before the person has chosen any (tests pass their own; by default the
        library folder ~/.mentora switched on, the usual book folders and cloud drives offered but off)."""
        self.db, self.registry, self.pool, self.materials = db, registry, pool, materials
        self.offered = [(r, False, True) for r in roots] if roots is not None else candidate_roots()
        try:
            materials.library_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("could not create the library folder %s", materials.library_dir)
        scope = self.scope()
        self.index = ShelfIndex(directory, *self._effective(scope))
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

    def forget(self, text: str) -> list[str]:
        self.db.set_setting(INTERESTS_KEY, [x for x in self.interests() if x != text])
        return self.interests()

    # -- scope ----------------------------------------------------------------
    def scope(self) -> dict:
        """{"roots": [{path, enabled, cloud}], "exclude": [path]}; before any choice, the offered folders."""
        saved = self.db.get_setting(SCOPE_KEY)
        if saved:
            return saved
        return {"roots": [{"path": str(r), "enabled": on, "cloud": cloud} for r, cloud, on in self.offered], "exclude": []}

    @staticmethod
    def _effective(scope: dict) -> tuple[list[Path], list[Path]]:
        return ([Path(r["path"]) for r in scope["roots"] if r["enabled"] and Path(r["path"]).is_dir()],
                [Path(p) for p in scope["exclude"]])

    def scope_view(self) -> dict:
        def label(p: str) -> str:
            if p.endswith("com~apple~CloudDocs"):
                return "iCloud Drive"
            home = str(Path.home())
            return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p
        sc = self.scope()
        return {"roots": [{**r, "label": label(r["path"]), "exists": Path(r["path"]).is_dir(),
                           "library": Path(r["path"]) == self.materials.library_dir} for r in sc["roots"]],
                "exclude": [{"path": p, "label": label(p)} for p in sc["exclude"]]}

    @staticmethod
    def _folder(raw: str) -> str:
        p = Path(raw.strip()).expanduser()
        if not p.is_absolute() or not p.is_dir():
            raise MaterialError("bad_folder", f"找不到這個資料夾：{raw}")
        if p == Path(p.anchor):
            raise MaterialError("bad_folder", "不能把整個磁碟設為搜尋範圍，請選擇其中的資料夾。")
        return str(p.resolve())

    async def set_scope(self, roots: list[dict], exclude: list[str]) -> dict:
        """Save the person's scope, forget what lies outside it, walk the new scope and choose a new shelf."""
        clouds = {r["path"]: r.get("cloud", False) for r in self.scope()["roots"]}
        new_roots, seen = [], set()
        for r in roots:
            path = self._folder(r["path"]) if r.get("enabled") or r["path"] not in clouds else r["path"]
            if path not in seen:
                seen.add(path)
                new_roots.append({"path": path, "enabled": bool(r.get("enabled")), "cloud": clouds.get(r["path"], False)})
        scope = {"roots": new_roots, "exclude": list(dict.fromkeys(self._folder(p) for p in exclude))}
        self.db.set_setting(SCOPE_KEY, scope)
        if self.preparing:
            self.task.cancel()
        self.index.configure(*self._effective(scope))
        await self._rescan()
        ids = {e.id for e in self.index.entries}
        self.meta.keep_only(ids)
        if self.result:
            self._save_result({**self.result, "books": [b for b in self.result["books"] if b["id"] in ids]})
        self.start(None)
        return self.scope_view()

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
                # a shelf saved before modes were recorded was chosen by the model; no run yet means keyword ranking
                "simulated": (r or {}).get("simulated", False), "mode": r.get("mode", "model") if r else "keywords",
                "indexed": len(self.index.entries), "scoped": bool(self.index.roots)}

    def quick(self, q: str) -> list[dict]:
        """Keyword search over the index, no model: what the shelf shows while the person is still typing. The wish is
        widened with the topic lexicon, and matched against file names, titles, folders and opening text."""
        kws, _ = expand(q)
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
            runtime = None   # the simulated backend cannot judge books: the lexicon and keywords do it instead
            if not self.registry.backend.simulated:
                snap = ModelSnapshot.from_config(self.registry.cfg)
                self.pool.seed(snap, self.registry.backend)    # one adapter (one OpenVINO pipeline) per physical model
                runtime = ModelRuntime(snap, self.pool)
            scout = ShelfScout(self.index, self.meta, runtime)
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
                               "books": out.get("books") or [], "simulated": self.registry.backend.simulated,
                               # did a model choose this shelf, or keywords (no model, or both model steps failed)?
                               "mode": "model" if runtime and len(out.get("model_failures") or []) < 2 else "keywords"})
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
