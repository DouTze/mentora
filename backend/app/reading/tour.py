"""導讀巡覽: the reader's 導讀, prepared only around the page being read (no whole-material study first).

Pressing 導讀 at page p takes the pages p−n … p+n (n chosen in 設定 › 導讀) as one *window*: a learning session over
that range (WorkflowService.start_from_material, marked `tour`), segmented and guided by the learning flow's own
nodes (learning_graph.segment_now / guide_unit) but driven from here rather than by the reader's commands — there is
no 下一段, question or quiz inside a window. Windows never overlap: a window is cut short where another one begins.
Windows that meet form a *run*, the stretch shown in the reader's 導讀 mode; scrolling past either end of the run opens
the window next to it (extend).

One worker runs one job at a time, for the book open in the reader — a window's segmentation or one 主題's guide —
always the job nearest the 主題 on screen (the focus): first the 主題 on the focus page, then the others outwards,
windows next to the focus window before those further off. When the focus moves away from the running job, the job is
cancelled and the nearest one starts; the segmentation batches and the 區塊 guides it had accepted are saved, so a later
run goes on from there. Leaving the mode (pause) or opening another book stops the worker the same way."""
from __future__ import annotations

import asyncio
import logging

from .. import pdf_worker
from ..db import Database
from ..llm.registry import Registry
from ..materials import MaterialStore
from ..persistence.repository import Repository

STRATEGY = "rules-v1"
LINES_PER_PAGE = 40      # a text material counts lines: n "pages" around the line being read are n × this many lines
MAX_SPAN = 10

log = logging.getLogger("mentor.tour")


def is_window(row: dict) -> bool:
    return bool((row.get("workflow_meta") or {}).get("tour"))


class TourError(Exception):
    def __init__(self, code: str, detail: str, status: int = 409):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


class GuideTour:
    def __init__(self, db: Database, registry: Registry, materials: MaterialStore, repo: Repository, workflow):
        self.db, self.registry, self.materials, self.repo, self.workflow = db, registry, materials, repo, workflow
        self._mid: str | None = None                 # the book whose 導讀 is being prepared
        self._focus: dict[str, dict] = {}            # book → {"session_id", "index" (主題 on screen) | None, "at"}
        self._worker: asyncio.Task | None = None
        self._job: asyncio.Task | None = None
        self._running: tuple | None = None           # ("segment", sid, None) | ("guide", sid, index)
        self._wake: asyncio.Event | None = None
        self._errors: dict[tuple, dict] = {}         # (sid, index | None) → why it failed; cleared by retry
        self._attempts: dict[tuple, int] = {}        # (sid, index) → the guide attempt to run next
        self._creating: dict[str, dict] = {}         # book → the window being set up {"start", "end"}
        self._lock: asyncio.Lock | None = None
        self._quotes: dict[str, list[dict]] = {}     # sid → its 主題 with the pages they cover (fixed once segmented)
        self._blank: dict[str, set] = {}             # book → ranges with no text to guide, between windows or at an edge
        # a window's start command is closed when the window is made; one a crash left open is closed now, before the
        # runner takes up pending commands (it must never run a window)
        for r in db.list_sessions():
            if is_window(r):
                for cmd in repo.active_commands(r["id"]):
                    repo.finish_command(r["id"], cmd["command_id"], status="completed", workflow_status={"waiting": "tour"})

    # -- windows -------------------------------------------------------------------------------
    def windows(self, mid: str) -> list[dict]:
        rows = [r for r in self.db.list_sessions() if is_window(r) and r["status"] != "closed"
                and (r["content_snapshot"].get("source") or {}).get("material_id") == mid]
        return sorted(rows, key=lambda r: r["content_snapshot"]["source"]["start"])

    @staticmethod
    def _range(r: dict) -> tuple[int, int]:
        src = r["content_snapshot"]["source"]
        return src["start"], src["end"]

    def _meet(self, mid: str, end: int, start: int) -> bool:
        """Whether a window ending at `end` and one starting at `start` meet: next to each other, or only blank
        pages between them."""
        return end + 1 == start or (end + 1, start - 1) in self._blank.get(mid, ())

    def _run_of(self, mid: str, rows: list[dict], sid: str | None) -> list[dict]:
        """The windows that meet the window `sid` on either side, in page order."""
        k = next((i for i, r in enumerate(rows) if r["id"] == sid), None)
        if k is None:
            return []
        lo, hi = k, k
        while lo > 0 and self._meet(mid, self._range(rows[lo - 1])[1], self._range(rows[lo])[0]):
            lo -= 1
        while hi + 1 < len(rows) and self._meet(mid, self._range(rows[hi])[1], self._range(rows[hi + 1])[0]):
            hi += 1
        return rows[lo:hi + 1]

    @staticmethod
    def _segmented(r: dict) -> bool:
        return (r["content_snapshot"].get("segmentation") or {}).get("status") == "done"

    def quotes(self, r: dict) -> list[dict]:
        """A segmented window's 主題, each with the first and last page (line) it covers: `from` / `to`."""
        sid = r["id"]
        if sid not in self._quotes:
            content = r["content_snapshot"]
            by_id = {b.block_id: b for b in self.repo.load_blocks(r["workflow_meta"]["snapshot_id"])}
            out = []
            for q in content["quotes"]:
                bs = [by_id[i] for i in q["block_ids"] if i in by_id]
                lo = min((b.locator.start for b in bs), default=content["source"]["start"])
                out.append({**q, "from": lo, "to": max((b.locator.end for b in bs), default=lo)})
            self._quotes[sid] = out
        return self._quotes[sid]

    def _span(self, m: dict, n: int) -> int:
        n = max(0, min(MAX_SPAN, int(n)))
        return n * LINES_PER_PAGE if m["unit"] == "line" else n

    async def _create(self, mid: str, start: int, end: int) -> str:
        self._creating[mid] = {"start": start, "end": end}
        try:
            if self.db.path == ":memory:":   # nothing a child process could open: here, in a thread (tests)
                res = await asyncio.to_thread(self._create_here, mid, start, end)
            else:   # MuPDF must not run on two threads of this process at once (see app.pdf_worker)
                res = await pdf_worker.run("studies", pdf_worker.create_window, self.db.path,
                                           self.registry.cfg.model_dump(), mid, start, end, STRATEGY)
        finally:
            self._creating.pop(mid, None)
        if res.get("error"):
            raise TourError(res.get("code") or "bad_range", res["error"], 400)
        return res["id"]

    async def _make(self, mid: str, m: dict, rows: list[dict], lo: int, hi: int, grow: str) -> str | None:
        """The window lo..hi. A range with no text (pages of figures only) grows away from the run — "before" to
        earlier pages, "after" to later ones, "both" — by its own length until it has text; one that meets the book's
        edge or another window first is remembered as blank. Returns the session id, or None when blank."""
        floor = max((self._range(r)[1] for r in rows if self._range(r)[1] < lo), default=0) + 1
        ceil = min((self._range(r)[0] for r in rows if self._range(r)[0] > hi), default=m["unit_count"] + 1) - 1
        step = hi - lo + 1
        while True:
            try:
                return await self._create(mid, lo, hi)
            except TourError as e:
                if e.code != "no_text":
                    raise
            nlo = max(floor, lo - step) if grow in ("before", "both") else lo
            nhi = min(ceil, hi + step) if grow in ("after", "both") else hi
            if (nlo, nhi) == (lo, hi):
                self._blank.setdefault(mid, set()).add((lo, hi))
                return None
            lo, hi = nlo, nhi

    def _create_here(self, mid: str, start: int, end: int) -> dict:
        try:
            row = self.workflow.start_from_material(mid, start, end, STRATEGY, extra_meta={"tour": True}, notify=False)
        except Exception as e:  # noqa: BLE001 — an empty or unreadable range
            return {"error": getattr(e, "detail", None) or f"{type(e).__name__}: {e}", "code": getattr(e, "code", None)}
        self.repo.finish_command(row["id"], f"start-{row['id']}", status="completed", workflow_status={"waiting": "tour"})
        return {"id": row["id"]}

    def _lock_(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # -- what the reader does ------------------------------------------------------------------
    async def open(self, mid: str, at: int, n: int) -> dict:
        """導讀 pressed at page (line) `at`: the window covering it, made when there is none — p−n … p+n, cut short
        where another window begins — becomes the focus, and the book's 導讀 is prepared from there."""
        m = self.materials.get(mid, with_units=False)
        self.materials.require_ready(m)
        at = max(1, min(int(at), m["unit_count"]))
        async with self._lock_():
            rows = self.windows(mid)
            hit = next((r for r in rows if self._range(r)[0] <= at <= self._range(r)[1]), None)
            if hit is None:
                span = self._span(m, n)
                lo = max(1, at - span, max((self._range(r)[1] for r in rows if self._range(r)[1] < at), default=0) + 1)
                hi = min(m["unit_count"], at + span,
                         min((self._range(r)[0] for r in rows if self._range(r)[0] > at), default=m["unit_count"] + 1) - 1)
                sid = await self._make(mid, m, rows, lo, hi, "both")
                if sid is None:
                    raise TourError("no_text", "這附近沒有可導讀的文字。", 400)
            else:
                sid = hit["id"]
        self._focus[mid] = {"session_id": sid, "index": None, "at": at}
        await self._switch(mid)
        return self.view(mid)

    async def extend(self, mid: str, sid: str, direction: str, n: int) -> dict:
        """The reader scrolled past the first (last) 主題 of the run: the window before (after) it, 2n + 1 pages long
        (cut short where another window ends), becomes the focus — its 主題 nearest the run first."""
        m = self.materials.get(mid, with_units=False)
        async with self._lock_():
            rows = self.windows(mid)
            run = self._run_of(mid, rows, sid)
            if not run:
                raise TourError("not_found", "找不到這段導讀", 404)
            size = 2 * self._span(m, n)
            if direction == "before":
                hi = self._range(run[0])[0] - 1
                if hi < 1 or any(b[1] == hi and b[0] == 1 for b in self._blank.get(mid, ())):
                    return self.view(mid)
                lo = max(1, hi - size, max((self._range(r)[1] for r in rows if self._range(r)[1] < hi), default=0) + 1)
                at = hi
            else:
                lo = self._range(run[-1])[1] + 1
                if lo > m["unit_count"] or any(b == (lo, m["unit_count"]) for b in self._blank.get(mid, ())):
                    return self.view(mid)
                hi = min(m["unit_count"], lo + size,
                         min((self._range(r)[0] for r in rows if self._range(r)[0] > lo), default=m["unit_count"] + 1) - 1)
                at = lo
            new = await self._make(mid, m, rows, lo, hi, direction)
            if new is None:   # only blank pages that way: the run now ends there
                return self.view(mid)
        self._focus[mid] = {"session_id": new, "index": None, "at": at}
        await self._switch(mid)
        return self.view(mid)

    async def focus(self, mid: str, sid: str, index: int | None = None, at: int | None = None) -> dict:
        """The 主題 on screen (a window picked in the sidebar: its start). The job queue turns towards it."""
        if not any(r["id"] == sid for r in self.windows(mid)):
            raise TourError("not_found", "找不到這段導讀", 404)
        cur = self._focus.get(mid) or {}
        self._focus[mid] = {"session_id": sid, "index": index,
                            "at": at if at is not None else cur.get("at") if cur.get("session_id") == sid else None}
        await self._switch(mid)
        return self.view(mid)

    async def retry(self, mid: str, sid: str, index: int | None = None) -> dict:
        self._errors.pop((sid, index), None)
        await self._switch(mid)
        return self.view(mid)

    async def pause(self, mid: str) -> dict:
        """The reader left the 導讀 mode: what is running stops now (its saved work is kept) and nothing more starts."""
        if self._mid == mid:
            await self._stop()
        return self.view(mid)

    async def drop(self, mid: str) -> None:
        """The material is being deleted."""
        if self._mid == mid:
            await self._stop()
        self._focus.pop(mid, None)

    async def stop(self) -> None:
        """Shutdown."""
        await self._stop()

    # -- the worker ------------------------------------------------------------------------------
    async def _switch(self, mid: str) -> None:
        if self._mid != mid:
            await self._stop()
            self._mid = mid
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(mid))
        # a running job that is no longer the nearest to the focus gives way to it
        if self._job and not self._job.done() and self._running != self._next_job(mid):
            self._job.cancel()
        self._wake.set()

    async def _stop(self) -> None:
        self._mid = None
        w = self._worker
        self._worker = None
        if w and not w.done():
            w.cancel()
            await asyncio.gather(w, return_exceptions=True)

    async def _work(self, mid: str) -> None:
        assert self._wake is not None
        while self._mid == mid:
            job = self._next_job(mid)
            if job is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            self._running = job
            self._job = asyncio.create_task(self._do(job))
            try:
                await asyncio.wait({self._job})
            finally:
                if not self._job.done():   # the worker itself was cancelled (pause, another book)
                    self._job.cancel()
                    await asyncio.gather(self._job, return_exceptions=True)
                self._running = None

    def _next_job(self, mid: str) -> tuple | None:
        f = self._focus.get(mid)
        if not f:
            return None
        run = self._run_of(mid, self.windows(mid), f["session_id"])
        k = next((i for i, r in enumerate(run) if r["id"] == f["session_id"]), None)
        if k is None:
            return None
        order = [run[k]] + [r for d in range(1, len(run)) for r in (run[k + d] if k + d < len(run) else None,
                                                                     run[k - d] if k - d >= 0 else None) if r]
        for r in order:
            sid = r["id"]
            if not self._segmented(r):
                if (sid, None) in self._errors:
                    continue
                return ("segment", sid, None)
            quotes = self.quotes(r)
            guides = r["state"]["reading"]["guides"]
            todo = [i for i in range(len(quotes)) if str(i) not in guides and (sid, i) not in self._errors]
            if not todo:
                continue
            if r is run[k]:
                todo.sort(key=self._priority(quotes, f))
            elif self._range(r)[0] < self._range(run[k])[0]:
                todo.sort(reverse=True)   # a window above the focus: its last 主題 is the nearest
            return ("guide", sid, todo[0])
        return None

    @staticmethod
    def _priority(quotes: list[dict], f: dict):
        """主題 on the focus page (or the 主題 on screen) first, in order; then the others by distance, the following
        one before the preceding one at the same distance."""
        if f.get("index") is not None and 0 <= f["index"] < len(quotes):
            on = {f["index"]}
        else:
            at = f.get("at")
            on = {q["index"] for q in quotes if at is not None and q["from"] <= at <= q["to"]}
            if not on and quotes:   # a page with no text: the 主題 just before it, else the first
                before = [q["index"] for q in quotes if at is not None and q["to"] < at]
                on = {before[-1] if before else 0}
        lo, hi = (min(on), max(on)) if on else (0, 0)
        return lambda i: (0, i, False) if i in on else (1, lo - i if i < lo else i - hi, i < lo)

    async def _do(self, job: tuple) -> None:
        kind, sid, idx = job
        graph = self.workflow.runner.graph if self.workflow.runner and self.workflow.runner.active else None
        try:
            if graph is None:
                out = {"error": {"code": "runner_unavailable", "detail": "背景工作程序沒有啟動，無法準備導讀。", "retryable": True}}
            elif kind == "segment":
                out = await graph.segment_now(sid)
            else:
                out = await graph.guide_unit(sid, idx, self._attempts.get((sid, idx), 1))
                if out.get("guide_attempt"):
                    self._attempts[(sid, idx)] = max(out["guide_attempt"].values())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — shown on the 主題; the person can try again
            log.warning("tour job %s failed", job, exc_info=True)
            out = {"error": {"code": "internal", "detail": f"{type(e).__name__}: {e}", "retryable": True}}
        err = out.get("error")
        if not err:   # nothing may be taken up again and again without progress
            row = self.db.get_session(sid)
            done = row and (self._segmented(row) if kind == "segment" else str(idx) in row["state"]["reading"]["guides"])
            if not done:
                err = {"code": "internal", "detail": "導讀沒有完成", "retryable": True}
        if err:
            self._errors[(sid, idx)] = err

    # -- what the reader shows ---------------------------------------------------------------------
    def view(self, mid: str) -> dict:
        m = self.materials.get(mid, with_units=False)
        f = self._focus.get(mid) or {}
        rows = self.windows(mid)
        run = self._run_of(mid, rows, f.get("session_id"))
        creating = self._creating.get(mid)
        blank = self._blank.get(mid, set())
        return {"material_id": mid, "active": self._mid == mid and bool(self._worker and not self._worker.done()),
                "focus": {k: f.get(k) for k in ("session_id", "index", "at")} if f else None,
                "creating": creating,
                "windows": [self._window_view(r) for r in run],
                "first": not run or self._range(run[0])[0] <= 1 or (1, self._range(run[0])[0] - 1) in blank,
                "last": not run or self._range(run[-1])[1] >= m["unit_count"]
                or (self._range(run[-1])[1] + 1, m["unit_count"]) in blank}

    def _state(self, job: tuple) -> str:
        sid, idx = job[1], job[2]
        if (sid, idx) in self._errors:
            return "failed"
        if self._running == job:
            return "working"
        return "queued"

    def _window_view(self, r: dict) -> dict:
        sid = r["id"]
        content = r["content_snapshot"]
        start, end = self._range(r)
        base = {"session_id": sid, "start": start, "end": end, "label": content["source"].get("label")}
        if not self._segmented(r):
            return {**base, "status": "segmenting", "state": self._state(("segment", sid, None)),
                    "error": self._errors.get((sid, None)), "topics": [], "skipped": []}
        guides = r["state"]["reading"]["guides"]
        topics = []
        for q in self.quotes(r):
            i = q["index"]
            g = guides.get(str(i))
            topics.append({"index": i, "unit_id": q.get("unit_id"), "label": q["label"], "heading": q.get("heading"),
                           "from": q["from"], "to": q["to"], "excerpt": q.get("excerpt"), "blocks": len(q.get("chunks") or []),
                           "guide": g, "state": "done" if g else self._state(("guide", sid, i)),
                           "error": None if g else self._errors.get((sid, i))})
        skipped = (content.get("segmentation") or {}).get("skipped") or []
        return {**base, "status": "ready", "state": "done", "error": None, "topics": topics,
                "skipped": [{k: x.get(k) for k in ("role", "label", "text")} for x in skipped]}

    # -- questions from the reader -----------------------------------------------------------------
    def part_at(self, mid: str, n: int) -> tuple[dict, list[dict], None] | None:
        """(window, its 主題 with their pages, None) for the segmented window covering page / line n, else None —
        what StudyService.ask needs from a study."""
        for r in self.windows(mid):
            lo, hi = self._range(r)
            if lo <= n <= hi and self._segmented(r):
                qs = self.quotes(r)
                return (r, qs, None) if qs else None
        return None
