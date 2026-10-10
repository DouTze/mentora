"""LangGraph sessions behind the existing API: creation with immutable snapshots, the events API, command status
and the extra fields of the session view. Legacy sessions are untouched (TutorService)."""
from __future__ import annotations

import shutil

from .. import config
from ..db import now_iso
from ..llm.runtime import BackendPool, ModelSnapshot
from ..persistence.repository import Repository
from ..reading.agent import READING_WORKFLOW_VERSION
from ..reading.contracts import CONTRACT_VERSION, TaskContract
from ..reading.pdf_evidence import attach_evidence
from ..reading.prompts import load_bundle
from ..reading.segmentation import rules_groups, plan_parts, to_quotes, SEGMENTATION_VERSION
from ..reading.schemas import SegmentationPlan, SourceUnit
from ..reading.source import SourceError, extract_blocks
from ..tutor import generate, reading
from ..tutor.reading_agent import ReadingWorkspace
from .commands import CommandError, new_command_id, payload_hash, validate
from .learning_graph import STATE_SCHEMA_VERSION, WORKFLOW_VERSION

LEGACY_STAGE = {"segment": "segment", "analyze": "guide", "context": "guide", "draft": "guide", "review": "guide",
                "revise": "guide", "guide": "guide", "compact": "compact", "generate": "generate",
                # reading-graph-v2 stages (R3–R6)
                "symbols": "guide", "understand": "guide", "source_audit": "guide", "revise_source": "guide",
                "plan_bridges": "guide", "body_audit": "guide", "verify": "guide", "revise_body": "guide",
                "notes": "compact", "notes_audit": "compact", "notes_revise": "compact",
                "quiz_generate": "generate", "quiz_audit": "generate"}


class WorkflowService:
    def __init__(self, svc, repo: Repository, pool: BackendPool, runner=None):
        self.svc, self.repo, self.pool, self.runner = svc, repo, pool, runner
        self.db = svc.db

    # -- sessions ---------------------------------------------------------------------------
    async def delete(self, sid: str) -> None:
        """Delete a learning record (legacy or LangGraph) with everything it owns: turns, commands, model call
        trace, its artifacts, checkpoints and reading workspace. Refused while a command is queued or running."""
        from ..tutor.session import SessionError
        if not self.db.get_session(sid):
            raise SessionError("not_found", "找不到學習紀錄")
        if self.runner:
            await self.runner.stop_background(sid)
        if self.repo.active_commands(sid) or (self.runner and self.runner.busy(sid)):
            raise SessionError("busy", "這段學習還有操作正在進行，請等它完成後再刪除")
        self.repo.delete_session(sid)
        if self.runner:
            await self.runner.drop_checkpoints(sid)
        shutil.rmtree(self.svc.reading_dir / sid, ignore_errors=True)

    async def close_material(self, material_id: str) -> list[str]:
        """The unfinished readings of a material being deleted: their work stops (queued or running commands end as
        cancelled) and they are closed. A closed reading stays a learning record to look at, but it is no longer in
        progress and takes no more commands. Returns the closed session ids."""
        closed = []
        for s in self.db.list_sessions():
            src = s["content_snapshot"].get("source") or {}
            if s["status"] != "active" or src.get("material_id") != material_id or (s.get("workflow_meta") or {}).get("study"):
                continue
            sid = s["id"]
            if self.runner:
                await self.runner.pause(sid)
            for cmd in self.repo.active_commands(sid):
                self.repo.finish_command(sid, cmd["command_id"], status="failed",
                                         error={"code": "material_deleted", "detail": "教材已刪除"})
            with self.db.conn() as c:
                c.execute("UPDATE sessions SET status='closed', updated_at=? WHERE id=?", (now_iso(), sid))
            if self.runner:
                self.runner.release(sid)   # nothing is left to run; forget the hold
            closed.append(sid)
        return closed

    def close_orphans(self, existing: set[str]) -> list[str]:
        """At start, before the runner takes up pending commands: unfinished readings (studies included) of materials
        deleted before deleting closed them are closed now, their queued commands ended. Returns the closed ids."""
        closed = []
        for s in self.db.list_sessions():
            mid = (s["content_snapshot"].get("source") or {}).get("material_id")
            if s["status"] != "active" or not mid or mid in existing:
                continue
            for cmd in self.repo.active_commands(s["id"]):
                self.repo.finish_command(s["id"], cmd["command_id"], status="failed",
                                         error={"code": "material_deleted", "detail": "教材已刪除"})
            with self.db.conn() as c:
                c.execute("UPDATE sessions SET status='closed', updated_at=? WHERE id=?", (now_iso(), s["id"]))
            closed.append(s["id"])
        return closed

    def start_at(self, study, material_id: str, n: int, strategy_id: str) -> dict:
        """The reader's 導讀 of the part (from the companion's whole-material study) that covers page or line `n`.
        Once the study is segmented, the reading takes the part's units from it (start_from_study): no extraction or
        segmentation again, and the guides any reading already wrote for those units are shown as they are. Before
        that, the page alone is read as choosing it and pressing 開始學習 does. An unfinished reading of the same part
        is taken up again instead of starting another."""
        found = study.part_at(material_id, n)
        start, end = (found[2]["from"], found[2]["to"]) if found else (n, n)
        snapshot = found[0]["workflow_meta"].get("snapshot_id") if found else None
        for s in self.db.list_sessions():
            src = s["content_snapshot"].get("source") or {}
            m = s.get("workflow_meta") or {}
            if (s["status"] != "completed" and s.get("workflow_runner") == "langgraph" and not m.get("study")
                    and src.get("material_id") == material_id and (src.get("start"), src.get("end")) == (start, end)
                    and (snapshot is None or m.get("snapshot_id") == snapshot)):   # not an older reading cut on its own
                return s
        if found:
            return self.start_from_study(found[0], found[2], strategy_id)
        return self.start_from_material(material_id, start, end, strategy_id)

    def start_from_study(self, study_row: dict, part: dict, strategy_id: str) -> dict:
        """A reading of one part of the material's study: the study's snapshot and the part's units (same unit ids
        and blocks, renumbered from 0), segmentation already done. `part` is a located part (StudyService.located)."""
        from ..tutor.session import SessionError
        strategy = self.svc.strategies.get(strategy_id)
        if not strategy:
            raise SessionError("not_found", "找不到教學策略")
        sc, sm = study_row["content_snapshot"], study_row["workflow_meta"]
        material_id, start, end = sc["source"]["material_id"], part["from"], part["to"]
        material, text = self.svc.materials.range_text(material_id, start, end)
        quotes = [dict(q) for q in sc["quotes"][part["quote_from"]:part["quote_to"] + 1]]
        for k, q in enumerate(quotes):
            q["id"], q["index"] = f"quote-{k + 1:02d}", k
        parts, _ = plan_parts(quotes)
        seg = sc.get("segmentation") or {}
        content = generate.empty_content(material, start, end, text)
        content.update(quotes=quotes, parts=parts,
                       segmentation={"planner": seg.get("planner"), "candidates": sum(len(q["block_ids"]) for q in quotes),
                                     "notes": [], "status": "done", "version": seg.get("version"),
                                     "reused_from": study_row["id"]})
        return self._create(material_id, material, start, end, content, strategy,
                            snapshot_id=sm["snapshot_id"], extraction_version=sm.get("extraction_version"),
                            planner=sm.get("planner") or config.QUOTE_PLANNER, extra_meta={"study_session": study_row["id"]})

    def start_from_material(self, material_id: str, start: int, end: int, strategy_id: str, *, depth: str | None = None,
                            goal: str | None = None, background: str | None = None,
                            extra_meta: dict | None = None, notify: bool = True) -> dict:
        from ..tutor.session import SessionError
        strategy = self.svc.strategies.get(strategy_id)
        if not strategy:
            raise SessionError("not_found", "找不到教學策略")
        material, text = self.svc.materials.range_text(material_id, start, end)   # size / empty checks as before
        _, units = self.svc.materials.range_units(material_id, start, end)
        try:
            snap, blocks = extract_blocks(material_id, material["unit"], start, end, units)
        except SourceError as e:
            raise SessionError(e.code, e.detail) from e
        if material.get("kind") == "pdf":   # R5: page / bbox / font evidence from the original file
            pdf_path, _ = self.svc.materials.file_path(material_id)
            if pdf_path.exists():
                blocks = attach_evidence(pdf_path, blocks, material.get("text_version") or 1)
        self.repo.save_snapshot(material_id, snap, blocks)
        # until the planner commits, the rule plan stands in (same as the legacy candidate paragraphs)
        groups = rules_groups(blocks)
        plan = SegmentationPlan(units=[SourceUnit(unit_id=f"pending-{i}", ordinal=i, block_ids=[blocks[j].block_id for j in g],
                                                  segmentation_version=SEGMENTATION_VERSION) for i, g in enumerate(groups)])
        quotes = to_quotes(material["unit"], plan, {b.block_id: b for b in blocks})
        parts, _ = plan_parts(quotes)
        content = generate.empty_content(material, start, end, text)
        planner = config.QUOTE_PLANNER
        content.update(quotes=quotes, parts=parts,
                       segmentation={"planner": planner, "candidates": len(blocks), "notes": [], "status": "pending"})
        return self._create(material_id, material, start, end, content, strategy, snapshot_id=snap["snapshot_id"],
                            extraction_version=snap["extraction_version"], planner=planner, depth=depth, goal=goal,
                            background=background, extra_meta=extra_meta, notify=notify)

    def _create(self, material_id: str, material: dict, start: int, end: int, content: dict, strategy: dict, *,
                snapshot_id: str, extraction_version: str | None, planner: str, depth: str | None = None,
                goal: str | None = None, background: str | None = None, extra_meta: dict | None = None,
                notify: bool = True) -> dict:
        """The learning session over `content` with its task contract and workflow meta, and its start command."""
        model = self.svc.registry.snapshot()
        msnap = ModelSnapshot.from_config(model)
        self.pool.seed(msnap, self.svc.registry.backend)   # one adapter (one OpenVINO pipeline) per physical model
        bundle = load_bundle()
        contract = TaskContract(scope={"material_id": material_id, "unit": material["unit"], "start": start, "end": end},
                                source_version=f"{snapshot_id}@{extraction_version}",
                                defaults=[k for k, v in (("depth", depth), ("goal", goal), ("reader_background", background)) if not v])
        contract = contract.model_copy(update={k: v for k, v in (("depth", depth), ("goal", goal),
                                                                 ("reader_background", (background or "").strip())) if v})
        meta = {"task_contract": contract.model_dump(), "contract_version": CONTRACT_VERSION,"runner": "langgraph", "workflow_version": WORKFLOW_VERSION, "state_schema_version": STATE_SCHEMA_VERSION,
                "reading_workflow_version": READING_WORKFLOW_VERSION, "prompt_bundle": bundle,
                "model_snapshot": {k: v for k, v in model.items() if k in ModelSnapshot.__dataclass_fields__},
                "model_config_hash": msnap.config_hash, "snapshot_id": snapshot_id, "planner": planner,
                "extraction_version": extraction_version, "guide_language": reading.guide_language(material),
                "output_language": reading.output_language(reading.guide_language(material), material.get("language")),
                **(extra_meta or {})}
        row = self.db.create_session(content, strategy, model, reading.initial_state(content),
                                     workflow_runner="langgraph", workflow_meta=meta)
        sid = row["id"]
        ReadingWorkspace(self.svc.reading_dir, sid).init(content)
        cid = f"start-{sid}"
        self.repo.insert_command(sid, cid, "start", {}, payload_hash("start", None, None, {}), None, client_supplied=False)
        if self.runner and notify:   # notify=False: called off the event loop; the caller notifies the runner
            self.runner.notify(sid)
        return row

    # -- events ------------------------------------------------------------------------------
    def submit(self, sid: str, body: dict) -> tuple[dict, bool]:
        """(command, created). Raises CommandError (409 / 400 / 404)."""
        row = self.db.get_session(sid)
        if not row:
            raise CommandError("not_found", "找不到學習紀錄", 404)
        if row.get("workflow_runner") != "langgraph":
            raise CommandError("legacy_session", "這筆學習使用舊版流程，請使用原本的操作。")
        if row["status"] == "closed":
            raise CommandError("closed", "這份教材已刪除，這段學習已結束，只能檢視。", 409)
        if self.runner is None or not self.runner.active:
            raise CommandError("runner_unavailable", "背景工作程序沒有啟動（可能有另一個後端正在執行），目前只能檢視。", 503)
        type_ = body.get("type")
        payload = dict(body.get("payload") or {})
        unit_id, question_id = body.get("unit_id"), body.get("question_id")
        cid = body.get("command_id") or None
        client = cid is not None
        cid = cid or new_command_id()
        h = payload_hash(type_, unit_id, question_id, payload)
        existing = self.repo.get_command(sid, cid)
        if existing:
            if existing["payload_hash"] != h:
                raise CommandError("command_conflict", "同一個操作編號已用於不同的內容。")
            return existing, False
        stored = validate(row, type_, unit_id, question_id, payload, body.get("expected_revision"),
                          self.repo.active_commands(sid), lambda c: self.repo.get_command(sid, c) if c else None)
        cmd, created = self.repo.insert_command(sid, cid, type_, stored, h, body.get("expected_revision"), client_supplied=client)
        if created and self.runner:
            self.runner.notify(sid)
        return cmd, created

    def command_view(self, cmd: dict) -> dict:
        return {"command_id": cmd["command_id"], "type": cmd["type"], "status": cmd["status"],
                "stage": cmd.get("stage"), "result": cmd.get("result"), "error": cmd.get("error"),
                "retryable": cmd.get("retryable", False), "created_at": cmd["created_at"], "updated_at": cmd["updated_at"]}

    # -- view ------------------------------------------------------------------------------------
    def workflow_view(self, row: dict) -> dict:
        sid = row["id"]
        active = self.repo.active_commands(sid)
        ws = row.get("workflow_status") or {}
        a = self.command_view(active[0]) if active else None
        m = row.get("workflow_meta") or {}
        return {"waiting": None if active else ws.get("waiting"), "stage": (a or {}).get("stage"),
                "active_command": a, "error": None if active else ws.get("error"),
                "failed_command_id": None if active else ws.get("failed_command_id"),
                "last_failed": None if active else ws.get("last_failed"),
                "failed_node": None if active else ws.get("failed_node"),
                "runner_active": bool(self.runner and self.runner.active),
                "workflow_version": m.get("workflow_version"), "prompt_bundle_hash": (m.get("prompt_bundle") or {}).get("hash"),
                "model_config_hash": m.get("model_config_hash"),
                "reading_workflow_version": m.get("reading_workflow_version"),
                "task_contract": {k: (m.get("task_contract") or {}).get(k) for k in ("depth", "goal", "reader_background", "defaults")}
                if m.get("task_contract") else None,
                "migrations": [{k: x.get(k) for k in ("from", "to", "at", "note")} for x in m.get("migrations") or []]}

    def job_view(self, wf: dict) -> dict | None:
        """The legacy `reading.job` shape, so the existing page keeps working while a command runs."""
        a = wf.get("active_command")
        if a and a["type"] not in ("ask", "answer"):
            st = a.get("stage") or ("segment" if a["type"] == "start" else "guide")
            return {"stage": LEGACY_STAGE.get(st, "guide"), "detail_stage": st, "error": None}
        if wf.get("waiting") == "retry" and wf.get("error"):
            st = {"prepare": "segment", "guide": "guide", "compact": "compact", "generate": "generate"}.get(wf.get("failed_node"), "guide")
            return {"stage": st, "detail_stage": wf.get("failed_node"), "error": wf["error"]}
        return None
