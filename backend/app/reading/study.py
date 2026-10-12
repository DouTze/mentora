"""導師全文學習: the companion reads the whole material once through the normal learning flow — exactly what choosing
every page and pressing 開始學習 does (WorkflowService.start_from_material(1..N), segmented by the session's own
prepare node). That session is kept as the material's study: the reader page reads its units and parts to know which
part the person is on, and a 導讀 started from the reader takes its units from it instead of segmenting again.
Nothing here splits text; it only reads what the learning flow produced.

The companion reads only along with the person: a study runs while its book is open in the reader (the page says so
with `focus` and a heartbeat) and is paused — held by the runner, its running command left to resume from its last
checkpoint — when the person leaves or opens another book. The person can also pause it, resume it, or cancel it
(the study is deleted and not started again until asked).

Also here: questions asked from the reader (no learning session), answered from the units on the page being read
and the guide of those units when a reading has already produced one."""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid

from langchain_core.messages import HumanMessage

from .. import config, pdf_worker
from ..db import Database, now_iso
from ..llm.registry import Registry
from ..llm.runtime import BackendPool, CallBudget, ModelRuntime, ModelSnapshot, stable_hash
from ..llm.tasks import TaskContext, run_structured
from ..materials import MaterialStore
from ..persistence.repository import DbSink, Repository
from ..tutor import reading
from .prompts import render
from .segmentation import OUTLINE_BATCH, TOPIC_BATCH, make_chunks

ASK_CHARS = 6000          # source text given with a reader question (the units on the page, then their neighbours)
HISTORY_ITEMS = 4
STRATEGY = "rules-v1"
BEAT_TIMEOUT_S = 45       # a reader that stopped sending its heartbeat (window closed, laptop asleep) has left
PREFS = "reader_study"    # setting: {material id: "paused" | "off"} chosen by the person

SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_questions (
  id TEXT PRIMARY KEY,
  material_id TEXT NOT NULL,
  unit_no INTEGER NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  label TEXT,
  unit_ids TEXT NOT NULL,
  used_guide INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reader_questions_material ON reader_questions (material_id, created_at);
"""


def is_study(row: dict) -> bool:
    return bool((row.get("workflow_meta") or {}).get("study"))


class StudyService:
    def __init__(self, db: Database, registry: Registry, pool: BackendPool, materials: MaterialStore, repo: Repository,
                 workflow):
        self.db, self.registry, self.pool, self.materials, self.repo, self.workflow = db, registry, pool, materials, repo, workflow
        self._located: dict[tuple[str, int], tuple[list[dict], list[dict]]] = {}   # (session, revision) → quotes, parts
        self._sids: dict[str, str] = {}                  # material → its study session
        self._starting: dict[str, asyncio.Task] = {}
        self._start_errors: dict[str, str] = {}
        self._blocks_cache: dict = {}
        self._chunk_cache: dict = {}
        self._focus: str | None = None
        self._kicks: set[asyncio.Task] = set()
        self._beat = 0.0
        self._watch: asyncio.Task | None = None
        self.tour = None   # app.reading.tour.GuideTour: its windows answer for pages no study covers
        with db.conn() as c:
            c.executescript(SCHEMA)
        # nobody is reading yet: every study waits (the runner recovers pending commands at start-up otherwise)
        if workflow.runner:
            for r in db.list_sessions():
                if is_study(r):
                    mid = (r["content_snapshot"].get("source") or {}).get("material_id")
                    if mid and (mid not in self._sids or r["created_at"] > (db.get_session(self._sids[mid]) or {}).get("created_at", "")):
                        self._sids[mid] = r["id"]
                    workflow.runner.hold(r["id"])

    # -- the study session --------------------------------------------------------------------
    def session(self, mid: str) -> dict | None:
        """The newest study of the material (a whole-material learning session)."""
        sid = self._sids.get(mid)
        row = self.db.get_session(sid) if sid else None
        if row is None:
            rows = [r for r in self.db.list_sessions() if is_study(r)
                    and (r["content_snapshot"].get("source") or {}).get("material_id") == mid]
            row = max(rows, key=lambda r: r["created_at"]) if rows else None
            if row:
                self._sids[mid] = row["id"]
        return row

    async def ensure(self, mid: str) -> dict | None:
        """Start the study when there is none (None while it is being set up: a long PDF takes a while to extract,
        which runs off the event loop); a study whose segmentation failed on a model error is retried. A book whose
        text is still being read after its import (app.material_extract) is not started yet."""
        if self._text_status(mid) != "ready":
            return None
        row = self.session(mid)
        if row is None:
            if self.pref(mid) == "off":
                return None
            t = self._starting.get(mid)
            if t is None or t.done():
                self._starting[mid] = asyncio.create_task(self._start(mid))
            return None
        ws = row.get("workflow_status") or {}
        if (ws.get("waiting") == "retry" and ws.get("failed_node") == "prepare" and ws.get("failed_command_id")
                and not self.repo.active_commands(row["id"]) and (ws.get("error") or {}).get("retryable")):
            try:
                self.workflow.submit(row["id"], {"type": "retry", "payload": {"target_command_id": ws["failed_command_id"]}})
            except Exception:  # noqa: BLE001 — the runner may be unavailable; the view still reports the failure
                pass
        return row

    async def _start(self, mid: str) -> None:
        m = self.materials.get(mid, with_units=False)
        try:
            if self.db.path == ":memory:":   # nothing a child process could open: in a thread (tests)
                row = await asyncio.to_thread(self.workflow.start_from_material, mid, 1, m["unit_count"], STRATEGY,
                                              extra_meta={"study": True}, notify=False)
            else:   # a long book's extraction runs MuPDF for minutes: in a process of its own (see app.pdf_worker)
                sid = await pdf_worker.run("studies", pdf_worker.create_study, self.db.path,
                                           self.registry.cfg.model_dump(), mid, m["unit_count"], STRATEGY)
                row = self.db.get_session(sid)
        except Exception as e:  # noqa: BLE001 — shown on the reader; the next visit tries again
            self._start_errors[mid] = getattr(e, "detail", None) or f"{type(e).__name__}: {e}"
            return
        self._start_errors.pop(mid, None)
        self._sids[mid] = row["id"]
        r = self.workflow.runner
        if r:
            if self._focus == mid and not self.pref(mid):
                r.release(row["id"])
            else:
                r.hold(row["id"])

    def kick(self, mid: str) -> None:
        """The book's text has just been read (app.material_extract): if it is the one open, start its study now
        rather than at the reader's next heartbeat. Called on the event loop."""
        if self._focus == mid and self.pref(mid) not in ("off", "paused"):
            t = asyncio.create_task(self.ensure(mid))
            self._kicks.add(t)               # kept until done (a task nobody holds may be collected)
            t.add_done_callback(self._kicks.discard)

    def _text_status(self, mid: str) -> str:
        try:
            return self.materials.get(mid, with_units=False).get("status") or "ready"
        except Exception:  # noqa: BLE001 — deleted: nothing to start
            return "gone"

    def view(self, mid: str) -> dict:
        """What the reader shows; starting and pausing happen through focus / leave / pause / resume / cancel."""
        text = self._text_status(mid)
        if text in ("extracting", "failed"):   # the book's text is still being read (or could not be): no study yet
            m = self.materials.get(mid, with_units=False)
            return {"status": "reading" if text == "extracting" else "failed", "session_id": None, "planner": None,
                    "progress": 0.0, "step": {"phase": "extract"} if text == "extracting" else None,
                    "error": m.get("status_detail") if text == "failed" else None, "parts": [], "paused": False}
        pref = self.pref(mid)
        row = self.session(mid)
        if row is None:
            err = self._start_errors.get(mid)
            starting = mid in self._starting and not self._starting[mid].done()
            status = "off" if pref == "off" else "failed" if err else "reading" if starting or self._focus == mid else "off"
            return {"status": status, "session_id": None, "planner": None, "progress": 0.0,
                    "step": {"phase": "extract"} if starting else None, "error": err, "parts": [], "paused": False}
        seg = row["content_snapshot"].get("segmentation") or {}
        ws = row.get("workflow_status") or {}
        failed = seg.get("status") != "done" and ws.get("failed_node") == "prepare" and not self.repo.active_commands(row["id"])
        status = "ready" if seg.get("status") == "done" else "failed" if failed else "reading"
        _, parts = self.located(row) if status == "ready" else ([], [])
        share, step = (1.0, None) if status == "ready" else self._progress(row)
        runner = self.workflow.runner
        # waiting for the person: paused by them, or held because the book is not the one being read
        paused = status == "reading" and bool(runner and runner.held(row["id"]))
        return {"status": status, "session_id": row["id"], "planner": seg.get("planner"), "progress": share, "step": step,
                "error": (ws.get("error") or {}).get("detail") if failed else None, "paused": paused,
                "by_user": pref == "paused",
                "parts": [{k: p.get(k) for k in ("index", "from", "to", "heading")} for p in parts]}

    # -- reading along with the person -------------------------------------------------------------
    def pref(self, mid: str) -> str | None:
        return (self.db.get_setting(PREFS) or {}).get(mid)

    def _set_pref(self, mid: str, value: str | None) -> None:
        prefs = dict(self.db.get_setting(PREFS) or {})
        if value:
            prefs[mid] = value
        else:
            prefs.pop(mid, None)
        self.db.set_setting(PREFS, prefs)

    async def focus(self, mid: str) -> dict:
        """The person is reading `mid` (also the heartbeat): its study runs, every other study is paused."""
        self._beat = time.monotonic()
        if self._focus != mid:
            self._focus = mid
            await self._pause_others(mid)
        if not self.pref(mid):
            row = await self.ensure(mid)
            if row and self.workflow.runner and self.workflow.runner.held(row["id"]):
                self.workflow.runner.release(row["id"])
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watchdog())
        return self.view(mid)

    async def leave(self, mid: str) -> None:
        if self._focus == mid:
            self._focus = None
            await self._pause(mid)

    async def _pause(self, mid: str) -> None:
        sid = self._sids.get(mid)
        if sid and self.workflow.runner:
            await self.workflow.runner.pause(sid)

    async def _pause_others(self, mid: str) -> None:
        for other in list(self._sids):
            if other != mid:
                await self._pause(other)

    async def _watchdog(self) -> None:
        while self._focus:
            await asyncio.sleep(BEAT_TIMEOUT_S / 3)
            if self._focus and time.monotonic() - self._beat > BEAT_TIMEOUT_S:
                await self.leave(self._focus)

    async def pause(self, mid: str) -> dict:
        self._set_pref(mid, "paused")
        await self._pause(mid)
        return self.view(mid)

    async def resume(self, mid: str) -> dict:
        """After a pause by the person, or a cancel: read along again (starting a new study when there is none)."""
        self._set_pref(mid, None)
        return await self.focus(mid)

    async def cancel(self, mid: str) -> dict:
        """Stop and delete the study; it is not started again until the person asks (resume)."""
        self._set_pref(mid, "off")
        t = self._starting.get(mid)
        if t and not t.done():
            await asyncio.gather(t, return_exceptions=True)
        row = self.session(mid)
        if row:
            await self._pause(mid)
            for cmd in self.repo.active_commands(row["id"]):
                self.repo.finish_command(row["id"], cmd["command_id"], status="failed",
                                         error={"code": "cancelled", "detail": "已取消全文學習"})
            await self.workflow.delete(row["id"])
            if self.workflow.runner:
                self.workflow.runner.release(row["id"])   # nothing left to run; forget the hold
            self._sids.pop(mid, None)
        return self.view(mid)

    async def drop(self, mid: str) -> None:
        """The material is being deleted: its study goes (as cancel does), with the person's setting for it and the
        questions asked from its pages."""
        await self.cancel(mid)
        self._set_pref(mid, None)
        self.forget(mid)
        if self._focus == mid:
            self._focus = None

    def _progress(self, row: dict) -> tuple[float, dict]:
        """How far the segmentation has got, read from the buffers the learning flow has saved (nothing is planned
        here). With the outline prompts the planner runs two passes over the 區塊: outlines in batches of
        OUTLINE_BATCH (a known count), then topic decisions in overlapping batches of TOPIC_BATCH (the count depends
        on where topics change, so it is estimated from the batches done). Without them: one pass of buffers."""
        m = row.get("workflow_meta") or {}
        if not m.get("snapshot_id"):
            return 0.0, {"phase": "extract"}
        blocks = self._blocks(m["snapshot_id"])
        wins = {a["payload"].get("key"): a["payload"] for a in self.repo.list_artifacts(row["id"], "segment_window")}
        if not blocks:
            return 0.0, {"phase": "extract"}
        two = "segment_outline" in ((m.get("prompt_bundle") or {}).get("prompts") or {})
        if not two:
            covered = sum(len(w.get("owned") or []) for w in wins.values())
            done = len(wins)
            share = min(0.99, covered / len(blocks))
            total = max(done + 1, round(done / share)) if share > 0 else None
            return round(share, 3), {"phase": "window", "current": min(done + 1, total or done + 1), "total": total, "approx": True}
        chunks = self._chunks(m["snapshot_id"], blocks)
        n = len(chunks)
        outline_total = max(1, math.ceil(n / OUTLINE_BATCH))
        o_done = sum(1 for k in wins if str(k).startswith("o"))
        t_wins = sorted((k for k in wins if str(k).startswith("t")))
        if o_done < outline_total:
            return round(0.5 * o_done / outline_total, 3), {"phase": "outline", "current": o_done + 1, "total": outline_total, "approx": False}
        if not t_wins:
            return 0.5, {"phase": "topic", "current": 1, "total": max(1, math.ceil(n / TOPIC_BATCH)), "approx": True}
        chunk_of = self._chunk_of(m["snapshot_id"], blocks, chunks)
        reached = max((chunk_of.get(i, 0) for k in t_wins for i in (wins[k].get("owned") or [])[-1:]), default=0) + 1
        t_done = len(t_wins)
        step = max(1.0, reached / t_done)
        total = t_done + (math.ceil((n - reached) / step) if reached < n else 0)
        share = 0.5 + 0.5 * min(0.99, reached / n)
        return round(share, 3), {"phase": "topic", "current": min(t_done + 1, max(total, t_done)), "total": max(total, t_done), "approx": True}

    def _blocks(self, snapshot_id: str) -> list:
        if self._blocks_cache.get("id") != snapshot_id:
            self._blocks_cache = {"id": snapshot_id, "blocks": self.repo.load_blocks(snapshot_id)}
        return self._blocks_cache["blocks"]

    def _chunks(self, snapshot_id: str, blocks: list) -> list[list[int]]:
        if snapshot_id not in self._chunk_cache:
            self._chunk_cache[snapshot_id] = make_chunks(blocks)
        return self._chunk_cache[snapshot_id]

    def _chunk_of(self, snapshot_id: str, blocks: list, chunks: list[list[int]]) -> dict[str, int]:
        key = snapshot_id + ":map"
        if key not in self._chunk_cache:
            self._chunk_cache[key] = {blocks[i].block_id: k for k, c in enumerate(chunks) for i in c}
        return self._chunk_cache[key]

    def located(self, row: dict) -> tuple[list[dict], list[dict]]:
        """The study's quotes and parts, each with the first and last page (line) it covers: `from` / `to`."""
        k = (row["id"], row["revision"])
        if k not in self._located:
            content = row["content_snapshot"]
            by_id = {b.block_id: b for b in self.repo.load_blocks(row["workflow_meta"]["snapshot_id"])}
            quotes = [dict(q) for q in content["quotes"]]
            for q in quotes:
                bs = [by_id[i] for i in q["block_ids"] if i in by_id]
                q["from"] = min((b.locator.start for b in bs), default=content["source"]["start"])
                q["to"] = max((b.locator.end for b in bs), default=q["from"])
            parts = [dict(p) for p in content["parts"]]
            for p in parts:
                qs = quotes[p["quote_from"]:p["quote_to"] + 1]
                p["from"], p["to"] = min(q["from"] for q in qs), max(q["to"] for q in qs)
                p["heading"] = next((q["heading"] for q in qs if q.get("heading")), None)
            self._located = {k: (quotes, parts)}
        return self._located[k]

    # -- where the reader is ----------------------------------------------------------------
    def part_at(self, mid: str, n: int) -> tuple[dict, list[dict], dict] | None:
        """(study session, its quotes, the part covering page / line n), or None while it is not segmented."""
        row = self.session(mid)
        if not row or (row["content_snapshot"].get("segmentation") or {}).get("status") != "done":
            return None
        quotes, parts = self.located(row)
        if not parts:
            return None
        best = next((p for p in parts if p["from"] <= n <= p["to"]), None)
        if best is None:   # a page with no text (a figure, a blank page): the part before it, else the first after
            before = [p for p in parts if p["to"] < n]
            best = before[-1] if before else parts[0]
        return row, quotes, best

    @staticmethod
    def quotes_at(quotes: list[dict], n: int) -> list[dict]:
        on = [q for q in quotes if q["from"] <= n <= q["to"]]
        if on:
            return on
        before = [q for q in quotes if q["to"] < n]
        return [before[-1]] if before else quotes[:1]

    # -- questions from the reader ------------------------------------------------------------
    def questions(self, mid: str, limit: int = 50) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM reader_questions WHERE material_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                             (mid, limit)).fetchall()
        return [self._qa(dict(r)) for r in reversed(rows)]

    def forget(self, mid: str) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM reader_questions WHERE material_id=?", (mid,))

    @staticmethod
    def _qa(d: dict) -> dict:
        d["unit_ids"] = json.loads(d["unit_ids"]) if isinstance(d["unit_ids"], str) else d["unit_ids"]
        d["used_guide"] = bool(d["used_guide"])
        return d

    def _guides(self, unit_ids: list[str]) -> dict[str, str]:
        """The newest accepted guide body of each unit, from any reading of this material."""
        if not unit_ids:
            return {}
        with self.db.conn() as c:
            rows = c.execute(f"SELECT unit_id, payload FROM reading_artifacts WHERE kind='guide' AND status='accepted' "
                             f"AND unit_id IN ({','.join('?' * len(unit_ids))}) ORDER BY created_at", unit_ids).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            g = (json.loads(r["payload"]) or {}).get("guide")
            if g:
                out[r["unit_id"]] = g
        return out

    async def ask(self, mid: str, n: int, question: str) -> dict:
        m = self.materials.get(mid)
        self.materials.require_ready(m)
        n = max(1, min(int(n), m["unit_count"]))
        found = self.part_at(mid, n) or (self.tour.part_at(mid, n) if self.tour else None)
        if found:
            _, quotes, _ = found
            on = self.quotes_at(quotes, n)
            # the units on the page first, then their neighbours while they fit
            picked, chars = list(on), sum(q["chars"] for q in on)
            lo, hi = on[0]["index"] - 1, on[-1]["index"] + 1
            while chars < ASK_CHARS and (lo >= 0 or hi < len(quotes)):
                for i in (hi, lo):
                    if 0 <= i < len(quotes) and chars + quotes[i]["chars"] <= ASK_CHARS:
                        picked.append(quotes[i])
                        chars += quotes[i]["chars"]
                lo, hi = lo - 1, hi + 1
            picked.sort(key=lambda q: q["index"])
            text = "\n\n".join(f"［{q['label']}］\n{q['text']}" for q in picked)
            unit_ids = [q["unit_id"] for q in on]
            label = on[0]["label"] if len(on) == 1 else f"{on[0]['label']}–{on[-1]['label']}"
        else:   # the companion has not finished reading the book: the page itself
            text = m["units"][n - 1][:ASK_CHARS] or "（這一頁沒有可讀取的文字）"
            unit_ids, label = [], None
        guides = self._guides(unit_ids)
        history = self.questions(mid, HISTORY_ITEMS)
        hist = "\n\n".join(f"問：{h['question']}\n答：{h['answer']}" for h in history) or "（還沒有問答）"
        where = f"第 {n} 頁" if m["unit"] == "page" else f"第 {n} 行附近"
        template = (config.PROMPT_DIR / "reader_ask.md").read_text(encoding="utf-8")
        prompt = render(template, {"material_title": m["title"], "where": where, "source": text,
                                   "guide": "\n\n".join(guides.values()) or "（這裡還沒有導讀）", "history": hist,
                                   "question": question})
        msnap = ModelSnapshot.from_config(self.registry.snapshot())
        self.pool.seed(msnap, self.registry.backend)
        op = f"reader:{mid}:{uuid.uuid4().hex[:10]}"
        ctx = TaskContext(ModelRuntime(msnap, self.pool), CallBudget.for_task("ask"), DbSink(self.repo), operation_key=op)
        ans, _ = await run_structured(ctx, [HumanMessage(content=prompt)], reading.Answer, task="ask", node="reader.ask",
                                      prompt_version=stable_hash(template, 12),
                                      meta={"quote_text": text, "question": question},
                                      source_ids=unit_ids, min_max_tokens=1024)
        rec = {"id": "rq-" + uuid.uuid4().hex[:12], "material_id": mid, "unit_no": n, "question": question,
               "answer": ans.answer, "label": label, "unit_ids": json.dumps(unit_ids), "used_guide": int(bool(guides)),
               "created_at": now_iso()}
        with self.db.conn() as c:
            c.execute("INSERT INTO reader_questions VALUES (:id,:material_id,:unit_no,:question,:answer,:label,:unit_ids,"
                      ":used_guide,:created_at)", rec)
        return self._qa(rec)
