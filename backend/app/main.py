"""FastAPI entry point. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

import json

from . import config, export, pdf_worker
from .db import Database
from .llm.registry import Registry
from .bookmarks import Bookmarks
from .library_describe import LibraryDescriber
from .materials import MaterialError, MaterialStore
from .llm.runtime import BackendPool
from .llm import remote
from .models import (AnswerRequest, AskRequest, BookmarkRequest, CategoryOrder, CategoryRequest, CompanionSettings, EventRequest, MaterialUpdate, ModelConfig,
                     ReaderAsk, ReaderLearn, RemoteEndpointRequest, ShelfImport, ShelfQuery, ShelfScope, StartSessionRequest)
from .persistence.repository import Repository
from .reading.study import StudyService
from .shelf.service import ShelfService
from .tutor.session import SessionError, TutorService
from .llm.tasks import TaskError
from .workflows.commands import CommandError
from .workflows.learning_graph import WorkflowDeps
from .workflows.runner import WorkflowRunner
from .workflows.service import WorkflowService


def checkpoint_path(db: Database) -> str:
    if config.CHECKPOINT_PATH:
        return config.CHECKPOINT_PATH
    return ":memory:" if db.path == ":memory:" else str(Path(db.path).with_name("checkpoints.sqlite3"))


def create_app(db: Database | None = None, registry: Registry | None = None, shelf: ShelfService | None = None) -> FastAPI:
    db = db or Database()
    registry = registry or Registry()
    remote.configure(db)
    saved = db.get_setting("model_config")
    if saved:
        registry.update(ModelConfig.model_validate(saved))
    materials = MaterialStore(db)
    bookmarks = Bookmarks(db)
    svc = TutorService(db, registry, materials)
    repo = Repository(db)
    pool = BackendPool(parallel=registry.cfg.parallel)
    runner = WorkflowRunner(WorkflowDeps(db=db, repo=repo, pool=pool, svc=svc), checkpoint_path(db))
    workflow = WorkflowService(svc, repo, pool, runner)
    svc.workflow = workflow
    study = StudyService(db, registry, pool, materials, repo, workflow)
    shelf = shelf or ShelfService(db, registry, pool, materials, config.SHELF_DIR)
    workflow.close_orphans({m["id"] for m in materials.list()})   # readings of materials deleted before that closed them
    describer = LibraryDescriber(registry, pool, materials)
    shelf.describer = describer
    try:   # the books imported before the book folder named copies after their content
        materials.adopt_library()
    except Exception:  # noqa: BLE001 — the folder may be unreadable; imports still work
        logging.getLogger("mentor.library").warning("could not adopt the book folder", exc_info=True)

    @asynccontextmanager
    async def lifespan(_app):
        # The runner owns LangGraph sessions: recover their pending commands now, stop cleanly on shutdown.
        await runner.start()
        # index the disk and prepare the shelf before the home page asks for it
        warm = asyncio.create_task(shelf.warmup()) if config.SHELF_WARMUP else None
        describer.sweep()     # books still without a description and an alias, when a model is connected
        try:
            yield
        finally:
            await describer.stop()
            for t in (warm, shelf.task):
                if t and not t.done():
                    t.cancel()
            await runner.stop()
            pdf_worker.shutdown()

    app = FastAPI(title="Mentor prototype", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.db, app.state.registry, app.state.svc = db, registry, svc
    app.state.repo, app.state.runner, app.state.workflow = repo, runner, workflow
    app.state.shelf, app.state.study, app.state.describer = shelf, study, describer

    @app.exception_handler(SessionError)
    async def _session_error(_, e: SessionError):
        status = {"not_found": 404, "completed": 409, "backend_changed": 409, "reading": 409, "not_reading": 409, "use_events": 409,
                  "legacy_readonly": 409, "busy": 409,
                  "model_unavailable": 502, "invalid_model_output": 502}.get(e.code, 400)
        return JSONResponse(status_code=status, content={"code": e.code, "detail": e.detail, "turn": e.turn,
                                                         "retryable": e.code in ("model_unavailable", "invalid_model_output")})

    @app.exception_handler(CommandError)
    async def _command_error(request, e: CommandError):
        sid = request.path_params.get("sid")
        session = None
        if sid and e.code not in ("not_found",):
            try:
                session = svc.view(sid).model_dump()
            except SessionError:
                session = None
        return JSONResponse(status_code=e.status, content={"code": e.code, "detail": e.detail, "retryable": False,
                                                           "session": session})

    @app.exception_handler(MaterialError)
    async def _material_error(_, e: MaterialError):
        return JSONResponse(status_code={"not_found": 404, "duplicate": 409}.get(e.code, 400),
                            content={"code": e.code, "detail": e.detail, "retryable": False, **e.extra})

    @app.get("/api/health")
    async def health():
        return {"ok": True, "backend": registry.backend.describe()}

    # -- library ------------------------------------------------------------
    @app.get("/api/content")
    async def contents():
        return [{"id": c["id"], "version": c["version"], "title": c["title"], "description": c.get("description", ""),
                 "concepts": len(c["concepts"]), "questions": len(c["questions"])} for c in svc.contents.values()]

    @app.get("/api/content/{cid}")
    async def content(cid: str):
        if cid not in svc.contents:
            raise HTTPException(404)
        return svc.contents[cid]

    @app.get("/api/strategies")
    async def strategies():
        return list(svc.strategies.values())

    # -- imported materials ---------------------------------------------------
    @app.get("/api/library")
    async def get_library():
        return {"categories": materials.list_categories(), "materials": materials.list()}

    @app.post("/api/categories")
    async def create_category(req: CategoryRequest):
        return materials.create_category(req.name)

    @app.put("/api/categories/order")
    async def reorder_categories(req: CategoryOrder):
        return materials.reorder_categories(req.ids)

    @app.patch("/api/categories/{cid}")
    async def rename_category(cid: str, req: CategoryRequest):
        return materials.rename_category(cid, req.name)

    @app.delete("/api/categories/{cid}")
    async def delete_category(cid: str):
        materials.delete_category(cid)
        return {"ok": True}

    @app.post("/api/materials")
    async def import_material(file: UploadFile = File(...), category_id: str | None = Form(None)):
        """A file from the browser (only its name is known, not its path). The same file again: 409 `duplicate` with
        the material it already is."""
        m = materials.add(file.filename or "untitled.txt", await file.read(), category_id)
        describer.schedule(m["id"])
        return m

    @app.get("/api/materials/{mid}")
    async def get_material(mid: str):
        return materials.get(mid)

    @app.patch("/api/materials/{mid}")
    async def update_material(mid: str, req: MaterialUpdate):
        return materials.update(mid, title=req.title, category_id=req.category_id,
                                set_category="category_id" in req.model_fields_set)

    @app.delete("/api/materials/{mid}")
    async def delete_material(mid: str):
        """The material and what only it needs (its study, the questions asked from its pages); its unfinished
        readings are closed and kept as learning records."""
        materials.get(mid, with_units=False)
        await study.drop(mid)
        closed = await workflow.close_material(mid)
        materials.delete(mid)
        bookmarks.forget(mid)
        return {"ok": True, "closed": closed}

    # -- bookmarks ---------------------------------------------------------------
    @app.get("/api/bookmarks")
    async def all_bookmarks():
        """Every material's bookmarks: {material_id: [{unit_no, chapter, created_at}]}."""
        return bookmarks.all()

    @app.put("/api/materials/{mid}/bookmarks/{n}")
    async def add_bookmark(mid: str, n: int, req: BookmarkRequest):
        m = materials.get(mid, with_units=False)
        if not 1 <= n <= m["unit_count"]:
            raise HTTPException(422, f"no {m['unit']} {n}")
        return bookmarks.add(mid, n, req.chapter)

    @app.delete("/api/materials/{mid}/bookmarks/{n}")
    async def remove_bookmark(mid: str, n: int):
        return bookmarks.remove(mid, n)

    @app.get("/api/materials/{mid}/cover")
    def material_cover(mid: str):
        path = materials.cover(mid)
        if not path:
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/materials/{mid}/pages")
    async def material_pages(mid: str):
        """[width, height] in points of every page (the reader lays the pages out before their images arrive)."""
        pdf = materials.pdf_of(mid)
        if not pdf:
            return {"sizes": []}
        cache = materials.sizes_cache(mid)
        if cache.exists():
            return {"sizes": json.loads(cache.read_text())}
        sizes = await pdf_worker.run("pages", pdf_worker.page_sizes, str(pdf))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(sizes))
        return {"sizes": sizes}

    @app.get("/api/materials/{mid}/pages/{n}")
    async def material_page(mid: str, n: int, w: int = Query(960, ge=200, le=4000)):
        t = materials.page_target(mid, n, w)
        if not t:
            raise HTTPException(404, "沒有這一頁")
        pdf, out = t
        if not out.exists():
            try:
                await pdf_worker.run("pages", pdf_worker.render_page, str(pdf), n, int(out.stem.split("-")[1]), str(out))
            except Exception as e:  # noqa: BLE001 — a page MuPDF cannot draw
                raise HTTPException(404, f"這一頁無法顯示：{e}") from e
        return FileResponse(out, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    async def cached_json(path: Path, job, *args) -> dict:
        """A pdf_worker job's JSON result, kept on disk next to the page images (written atomically)."""
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        out = await pdf_worker.run("pages", job, *args)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return out

    @app.get("/api/materials/{mid}/info")
    async def material_info(mid: str):
        """What a PDF holds besides its pages: metadata, page labels, outline, attachments, fonts, layers, form
        fields and counts (app.pdfdoc.inspect). Text materials have none."""
        pdf = materials.pdf_of(mid)
        if not pdf:
            raise HTTPException(404, "這份教材不是 PDF")
        try:
            return await cached_json(materials.info_cache(mid), pdf_worker.doc_info, str(pdf))
        except Exception as e:  # noqa: BLE001 — a file MuPDF cannot open
            raise HTTPException(422, f"無法讀取這個 PDF：{e}") from e

    @app.get("/api/materials/{mid}/pages/{n}/layer")
    async def material_page_layer(mid: str, n: int):
        """Page n's text runs (reading order), links, images and comments in view space (app.pdfdoc.layer)."""
        path = materials.layer_cache(mid, n)
        if not path:
            raise HTTPException(404, "沒有這一頁")
        pdf = materials.pdf_of(mid)
        try:
            return await cached_json(path, pdf_worker.page_layer, str(pdf), n)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"這一頁無法解析：{e}") from e

    @app.get("/api/materials/{mid}/images/{xref}")
    async def material_image(mid: str, xref: int):
        """A stored image of the PDF (an `xref` from a page layer), as png / jpeg / gif / webp."""
        pdf = materials.pdf_of(mid)
        if not pdf or xref <= 0:
            raise HTTPException(404, "沒有這張圖片")
        try:
            data, ext = await pdf_worker.run("pages", pdf_worker.image, str(pdf), xref)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(404, f"沒有這張圖片：{e}") from e
        return Response(content=data, media_type=f"image/{ext}", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/materials/{mid}/attachments/{i}")
    async def material_attachment(mid: str, i: int):
        """Attachment i of the document info's `attachments`, downloaded under its own name."""
        info = await material_info(mid)
        if not 0 <= i < len(info["attachments"]):
            raise HTTPException(404, "沒有這個附件")
        try:
            name, data = await pdf_worker.run("pages", pdf_worker.attachment, str(materials.pdf_of(mid)), info["attachments"][i]["source"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(404, f"無法取出這個附件：{e}") from e
        from urllib.parse import quote
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    # -- the reader: the companion's whole-material study, questions, 導讀 from where the reader is ----------
    @app.get("/api/materials/{mid}/study")
    def material_study(mid: str):
        return study.view(mid)

    @app.post("/api/materials/{mid}/study/{action}")
    async def material_study_action(mid: str, action: Literal["focus", "leave", "pause", "resume", "cancel"]):
        """focus (also the reader's heartbeat) / leave: the person opened or left the book; pause / resume / cancel:
        their own choice about the companion's whole-material study."""
        materials.get(mid, with_units=False)
        if action == "leave":
            await study.leave(mid)
            return study.view(mid)
        return await getattr(study, action)(mid)

    @app.get("/api/materials/{mid}/questions")
    def reader_questions(mid: str):
        return study.questions(mid)

    @app.post("/api/materials/{mid}/ask")
    async def reader_ask(mid: str, req: ReaderAsk):
        try:
            return await study.ask(mid, req.at, req.question.strip())
        except TaskError as e:
            raise HTTPException(503 if e.kind == "unavailable" else 502, e.detail) from e

    @app.post("/api/materials/{mid}/learn")
    async def reader_learn(mid: str, req: ReaderLearn):
        row = workflow.start_at(study, mid, req.at, req.strategy_id)
        return svc.view(row["id"])

    @app.get("/api/materials/{mid}/file")
    async def material_file(mid: str):
        path, m = materials.file_path(mid)
        if not path.exists():
            raise HTTPException(404)
        media = "application/pdf" if m["kind"] == "pdf" else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media, content_disposition_type="inline", filename=m["filename"])

    # -- model settings -----------------------------------------------------
    # -- home-page bookshelf ----------------------------------------------------
    @app.get("/api/shelf")
    def shelf_view():
        return shelf.view()

    @app.get("/api/shelf/search")
    def shelf_search(q: str = Query("", max_length=200)):
        return {"books": shelf.quick(q)}

    @app.post("/api/shelf/query")
    async def shelf_query(req: ShelfQuery):
        shelf.start(req.text.strip() or None)
        return await asyncio.to_thread(shelf.view)

    @app.get("/api/shelf/scope")
    def shelf_scope():
        return shelf.scope_view()

    @app.put("/api/shelf/scope")
    async def shelf_set_scope(req: ShelfScope):
        return await shelf.set_scope([r.model_dump() for r in req.roots], req.exclude)

    @app.delete("/api/shelf/interests")
    def shelf_forget(req: ShelfQuery):
        return {"interests": shelf.forget(req.text)}

    @app.get("/api/shelf/{eid}/cover")
    def shelf_cover(eid: str):
        path = shelf.cover(eid)
        if not path:
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/shelf/import")
    def shelf_import(req: ShelfImport):
        return shelf.import_books(req.ids, req.category_name)

    @app.get("/api/settings")
    async def get_settings():
        return {"config": registry.cfg.model_dump(), "active": registry.backend.describe(), "remote": remote.public()}

    @app.put("/api/settings")
    async def put_settings(cfg: ModelConfig):
        registry.update(cfg)
        pool.set_parallel(cfg.parallel)
        db.set_setting("model_config", cfg.model_dump())
        describer.sweep()
        return {"config": registry.cfg.model_dump(), "active": registry.backend.describe(), "remote": remote.public()}

    @app.put("/api/settings/remote")
    async def put_remote(req: RemoteEndpointRequest):
        """The remote model server (URL + token of the proxy). The token is stored server-side and never returned."""
        if req.url.strip() and not req.url.strip().lower().startswith(("http://", "https://")):
            raise HTTPException(400, "網址必須以 http:// 或 https:// 開頭")
        return {"remote": remote.save(req.url, req.token)}

    @app.get("/api/companions")
    async def get_companions():
        return db.get_setting("companions")  # null until first saved; the frontend seeds defaults

    @app.put("/api/companions")
    async def put_companions(req: CompanionSettings):
        if req.active not in req.companions:
            raise HTTPException(400, "active companion has no model config")
        cfg = req.companions[req.active]
        registry.update(cfg)
        pool.set_parallel(cfg.parallel)
        db.set_setting("model_config", cfg.model_dump())
        db.set_setting("companions", req.model_dump())
        describer.sweep()
        return {"companions": req.model_dump(), "config": cfg.model_dump(), "active": registry.backend.describe()}

    @app.post("/api/settings/check")
    async def check_settings():
        return {"active": registry.backend.describe(), "health": await registry.backend.health()}

    # -- sessions -----------------------------------------------------------
    @app.post("/api/sessions")
    async def start(req: StartSessionRequest):
        if req.material_id:
            if req.start is None or req.end is None:
                raise MaterialError("bad_range", "請指定學習範圍的起點與終點。")
            row = workflow.start_from_material(req.material_id, req.start, req.end, req.strategy_id,
                                               depth=req.reading_depth, goal=req.reading_goal,
                                               background=req.reader_background)
            return svc.view(row["id"])
        return svc.start(req.content_id, req.strategy_id)

    @app.get("/api/sessions")
    async def list_sessions():
        return svc.list()

    @app.get("/api/sessions/{sid}")
    async def get_session(sid: str):
        return svc.view(sid)

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        await workflow.delete(sid)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/answer")
    async def answer(sid: str, req: AnswerRequest, debug_invalid: bool = Query(False, include_in_schema=False)):
        return await svc.answer(sid, req.answer, debug_force_invalid=debug_invalid)

    # -- reading sessions: one asynchronous entry point (guide / ask / next / finish / answer / retry) --
    @app.post("/api/sessions/{sid}/events")
    async def post_event(sid: str, req: EventRequest):
        cmd, created = workflow.submit(sid, req.model_dump())
        body = {**workflow.command_view(cmd), "session": svc.view(sid).model_dump()}
        done = cmd["status"] in ("completed", "failed")
        return JSONResponse(status_code=200 if done else 202, content=body)

    @app.get("/api/sessions/{sid}/commands/{cid}")
    async def get_command(sid: str, cid: str):
        cmd = repo.get_command(sid, cid)
        if not cmd:
            raise HTTPException(404, "找不到這個操作")
        return workflow.command_view(cmd)

    @app.get("/api/sessions/{sid}/trace")
    async def get_trace(sid: str):
        """Developer trace: every model call of a LangGraph session (not shown to students)."""
        return {"llm_calls": [{k: v for k, v in c.items() if k != "request"} for c in repo.list_llm_calls(sid)],
                "commands": [workflow.command_view(c) for c in repo.list_commands(sid)]}

    @app.post("/api/sessions/{sid}/replay")
    async def replay(sid: str):
        return await svc.replay(sid)

    @app.get("/api/sessions/{sid}/export")
    async def export_session(sid: str, format: str = "json"):
        view = svc.view(sid).model_dump()
        if format == "md":
            return PlainTextResponse(export.to_markdown(view), media_type="text/markdown; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="session-{sid}.md"'})
        return PlainTextResponse(export.to_json(view), media_type="application/json; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="session-{sid}.json"'})

    return app


app = create_app()
