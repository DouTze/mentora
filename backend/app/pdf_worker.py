"""Work with PDFs that runs in child processes.

MuPDF (PyMuPDF) must not run on two threads of one process at once: rendering reader pages on the server's thread pool
while the companion's study extracts the same book on another thread crashed the process (SIGBUS). Here each job runs
in a process of its own, with its own MuPDF, so the server's own MuPDF use (on the event loop) is left exactly as it
was:

    pages     page sizes and page images for the reader (a small pool, one MuPDF per worker)
    studies   setting up a whole-material study: the normal `start_from_material(1..N)` of the learning flow, whose
              text and layout extraction of a long book takes minutes

The functions below are module-level so the pool can pickle them; each worker imports only what its job needs."""
from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_pages: ProcessPoolExecutor | None = None
_studies: ProcessPoolExecutor | None = None


def _pool(kind: str) -> ProcessPoolExecutor:
    global _pages, _studies
    ctx = multiprocessing.get_context("spawn")   # never fork a process that holds MuPDF / asyncio / sqlite state
    if kind == "pages":
        if _pages is None:
            _pages = ProcessPoolExecutor(max_workers=2, mp_context=ctx)
        return _pages
    if _studies is None:
        _studies = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    return _studies


async def run(kind: str, fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_pool(kind), fn, *args)


def shutdown() -> None:
    global _pages, _studies
    for p in (_pages, _studies):
        if p:
            p.shutdown(wait=False, cancel_futures=True)
    _pages = _studies = None


# -- jobs (run in the child) ---------------------------------------------------------------------
def page_sizes(path: str) -> list[list[float]]:
    import pymupdf
    with pymupdf.open(path) as doc:
        return [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in doc]


def render_page(path: str, n: int, width: int, out: str) -> bool:
    import pymupdf
    target = Path(out)
    with pymupdf.open(path) as doc:
        page = doc[n - 1]
        zoom = width / max(page.rect.width, 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp.png")
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(str(tmp))
        tmp.replace(target)
    return True


def create_study(db_path: str, model_config: dict, material_id: str, unit_count: int, strategy_id: str) -> str:
    """The learning flow's own session start over every unit, marked as the material's study. Returns the session id;
    the server's runner then runs it (no runner here)."""
    from .db import Database
    from .llm.registry import Registry
    from .llm.runtime import BackendPool
    from .materials import MaterialStore
    from .models import ModelConfig
    from .persistence.repository import Repository
    from .tutor.session import TutorService
    from .workflows.service import WorkflowService
    db = Database(db_path)
    registry = Registry(ModelConfig.model_validate(model_config))
    svc = TutorService(db, registry, MaterialStore(db))
    wf = WorkflowService(svc, Repository(db), BackendPool(), None)
    row = wf.start_from_material(material_id, 1, unit_count, strategy_id, extra_meta={"study": True}, notify=False)
    return row["id"]
