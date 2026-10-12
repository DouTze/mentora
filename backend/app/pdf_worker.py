"""Work with PDFs that runs in child processes.

MuPDF (PyMuPDF) must not run on two threads of one process at once: rendering reader pages on the server's thread pool
while the companion's study extracts the same book on another thread crashed the process (SIGBUS). Here each job runs
in a process of its own, with its own MuPDF, so the server's own MuPDF use (on the event loop) is left exactly as it
was:

    pages     page sizes, page images, page layers (text runs, links, images, comments), document info, stored
              images and attachments for the reader (a small pool, one MuPDF per worker; app.pdfdoc does the reading)
    studies   setting up a whole-material study: the normal `start_from_material(1..N)` of the learning flow, whose
              text and layout extraction of a long book takes minutes; and the windows of the reader's 導讀巡覽
    extract   reading an imported PDF's text, page by page (app.material_extract), one book at a time

The functions below are module-level so the pool can pickle them; each worker imports only what its job needs."""
from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_pages: ProcessPoolExecutor | None = None
_studies: ProcessPoolExecutor | None = None
_extract: ProcessPoolExecutor | None = None


def _pool(kind: str) -> ProcessPoolExecutor:
    global _pages, _studies, _extract
    ctx = multiprocessing.get_context("spawn")   # never fork a process that holds MuPDF / asyncio / sqlite state
    if kind == "pages":
        if _pages is None:
            _pages = ProcessPoolExecutor(max_workers=2, mp_context=ctx)
        return _pages
    if kind == "extract":
        if _extract is None:
            _extract = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
        return _extract
    if _studies is None:
        _studies = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    return _studies


async def run(kind: str, fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_pool(kind), fn, *args)


def shutdown() -> None:
    global _pages, _studies, _extract
    for p in (_pages, _studies, _extract):
        if p:
            p.shutdown(wait=False, cancel_futures=True)
    _pages = _studies = _extract = None


# -- jobs (run in the child) ---------------------------------------------------------------------
def extract_units(path: str) -> dict:
    """An imported PDF's text, one entry per page (MaterialStore.add reads it the same way when not deferred):
    {"units": [...]}, or {"error": why} for a file without text (MaterialError does not cross the process boundary:
    its constructor cannot be called back from pickled args, which breaks the pool)."""
    from .materials import MaterialError, extract_units as read
    try:
        return {"units": read("pdf", Path(path).read_bytes())}
    except MaterialError as e:
        return {"error": e.detail}


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


def doc_info(path: str) -> dict:
    import pymupdf

    from .pdfdoc import document_info
    with pymupdf.open(path) as doc:
        return document_info(doc)


def page_layer(path: str, n: int) -> dict:
    import pymupdf

    from .pdfdoc import page_layer as layer
    with pymupdf.open(path) as doc:
        return layer(doc, n)


def image(path: str, xref: int) -> tuple[bytes, str]:
    import pymupdf

    from .pdfdoc import image_bytes
    with pymupdf.open(path) as doc:
        return image_bytes(doc, xref)


def attachment(path: str, source: dict) -> tuple[str, bytes]:
    import pymupdf

    from .pdfdoc import embedded_file
    with pymupdf.open(path) as doc:
        return embedded_file(doc, source)


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


def create_window(db_path: str, model_config: dict, material_id: str, start: int, end: int, strategy_id: str) -> dict:
    """One window of the reader's 導讀巡覽 (app.reading.tour): the learning flow's session over units start..end (its
    text and PDF layout evidence extracted here), marked as a tour window. Its start command is closed at once: the
    tour segments the window and writes its guides itself, the runner never runs it. {"id": …}, or {"error": why}
    (a SessionError does not cross the process boundary)."""
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
    repo = Repository(db)
    wf = WorkflowService(svc, repo, BackendPool(), None)
    try:
        row = wf.start_from_material(material_id, start, end, strategy_id, extra_meta={"tour": True}, notify=False)
    except Exception as e:  # noqa: BLE001 — an empty or unreadable range: shown on the window
        return {"error": getattr(e, "detail", None) or f"{type(e).__name__}: {e}", "code": getattr(e, "code", None)}
    repo.finish_command(row["id"], f"start-{row['id']}", status="completed", workflow_status={"waiting": "tour"})
    return {"id": row["id"]}
