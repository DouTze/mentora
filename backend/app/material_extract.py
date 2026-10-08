"""Reading an imported PDF's text in the background.

Importing a book copies it and records it at once (its page count only); its text — read page by page, formulas
turned into LaTeX — takes from seconds to half a minute for a long book, so it is read afterwards, one book at a time,
in a child process (app.pdf_worker "extract": MuPDF is never run on two threads of the server, and the parsing does
not hold up the server's own work). Until it is read, the book opens and shows its pages; what needs its text (a
question, 導讀, the companion's study) waits (MaterialStore.require_ready). Books left half read when the app stopped
are read again at the next start (`resume`). When a book's text is ready, `on_ready(mid)` runs (its description by
the model is scheduled there)."""
from __future__ import annotations

import asyncio
import logging

from . import pdf_worker
from .materials import MaterialError, MaterialStore

log = logging.getLogger("mentor.extract")


class TextExtractor:
    def __init__(self, materials: MaterialStore, on_ready=None):
        self.materials = materials
        self.on_ready = on_ready
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queued: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._lock: asyncio.Lock | None = None

    def schedule(self, mid: str) -> None:
        """Read this material's text in the background. Safe to call from any thread (the shelf import runs in a
        worker thread): the work is handed to the app's event loop."""
        def start():
            if mid in self._queued:
                return
            self._queued.add(mid)
            t = asyncio.create_task(self._read(mid))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None or self._loop.is_closed():
                log.warning("no event loop to read %s on; it is read at the next start", mid)
                return
            self._loop.call_soon_threadsafe(start)
            return
        start()

    def resume(self) -> None:
        """At start (on the loop): read the books whose text was not finished."""
        self._loop = asyncio.get_running_loop()
        for mid in self.materials.extracting():
            self.schedule(mid)

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def idle(self) -> None:
        """Wait until every book scheduled so far is read (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _read(self, mid: str) -> None:
        self._lock = self._lock or asyncio.Lock()
        try:
            async with self._lock:   # one book at a time, in the order they came
                try:
                    path, m = self.materials.file_path(mid)
                except MaterialError:
                    return   # deleted while it waited
                if m.get("status") != "extracting":
                    return
                try:
                    if self.materials.db.path == ":memory:":   # nothing a child could share with the test: in a thread
                        from .materials import extract_units
                        units = await asyncio.to_thread(extract_units, "pdf", path.read_bytes())
                    else:
                        out = await pdf_worker.run("extract", pdf_worker.extract_units, str(path))
                        if "error" in out:
                            raise MaterialError("no_text", out["error"])
                        units = out["units"]
                except MaterialError as e:
                    self._fail(mid, e.detail)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — a damaged file, a crashed worker: the book says why
                    log.warning("could not read %s", mid, exc_info=True)
                    self._fail(mid, f"{type(e).__name__}: {e}")
                    return
                try:
                    self.materials.get(mid, with_units=False)
                except MaterialError:
                    return   # deleted while it was read
                self.materials.set_units(mid, units)
                log.info("read %s: %d pages", mid, len(units))
            if self.on_ready:
                try:
                    self.on_ready(mid)
                except Exception:  # noqa: BLE001
                    log.warning("on_ready failed for %s", mid, exc_info=True)
        finally:
            self._queued.discard(mid)

    def _fail(self, mid: str, detail: str) -> None:
        try:
            self.materials.set_failed(mid, detail)
        except Exception:  # noqa: BLE001 — deleted meanwhile
            pass
