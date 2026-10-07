"""Sessions: built-in content sessions (diagnostic questions answered here) and the view of every session.
Material (reading) sessions run in app/workflows (events API); sessions of the removed old reading flow stay
viewable and exportable, read-only."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..db import Database
from ..llm.base import LLMError
from ..llm.registry import Registry
from ..models import ModelJudgement, QuestionView, ReadingView, SessionView, TutorMessage
from ..materials import MaterialStore
from .. import config
from . import engine, library


class SessionError(Exception):
    def __init__(self, code: str, detail: str, *, turn: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.turn = turn


class InvalidModelOutput(Exception):
    pass


THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def parse_judgement(text: str, valid_concept_ids: set[str]) -> tuple[ModelJudgement, list[str]]:
    """Strict-ish JSON parsing. Returns (judgement, validation_notes)."""
    notes: list[str] = []
    cleaned = THINK_RE.sub("", text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except ValueError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            raise InvalidModelOutput("模型輸出不含 JSON 物件")
        try:
            data = json.loads(m.group(0))
            notes.append("JSON 前後夾雜其他文字，已擷取")
        except ValueError as e:
            raise InvalidModelOutput(f"JSON 解析失敗：{e}") from e
    try:
        j = ModelJudgement.model_validate(data)
    except ValidationError as e:
        raise InvalidModelOutput(f"欄位不符：{e.errors()[0]['msg']} @ {e.errors()[0]['loc']}") from e
    unknown = [s for s in j.source_ids if s not in valid_concept_ids]
    if unknown:
        notes.append(f"移除不存在的教材引用：{unknown}")
        j.source_ids = [s for s in j.source_ids if s in valid_concept_ids]
    return j, notes


class TutorService:
    def __init__(self, db: Database, registry: Registry, materials: MaterialStore | None = None,
                 reading_dir: Path | None = None):
        self.db = db
        self.registry = registry
        self.materials = materials or MaterialStore(db)
        self.contents = library.load_contents()
        self.strategies = library.load_strategies()
        self.reading_dir = reading_dir or (Path(db.path).parent / "reading" if db.path != ":memory:" else config.READING_DIR)
        self.workflow = None   # WorkflowService (material sessions), set by the app

    # -- session lifecycle --------------------------------------------------
    def start(self, content_id: str, strategy_id: str) -> SessionView:
        content = self.contents.get(content_id)
        strategy = self.strategies.get(strategy_id)
        if not content or not strategy:
            raise SessionError("not_found", "找不到教材或教學策略")
        row = self.db.create_session(content, strategy, self.registry.snapshot(), engine.initial_state(content))
        return self.view(row["id"])

    @staticmethod
    def _legacy_only(row: dict) -> None:
        if row.get("workflow_runner") == "langgraph":
            raise SessionError("use_events", "這筆學習使用新版流程，請改用 /api/sessions/{id}/events。")
        if (row.get("content_snapshot") or {}).get("quotes") and (row.get("state") or {}).get("phase") == "reading":
            # the old reading flow is removed: its unfinished reading cannot continue (its quiz, judged by the same
            # code as the reading workflow's, and replays of it still can)
            raise SessionError("legacy_readonly", "這筆教材學習是用已移除的舊版流程建立的，導讀無法繼續，只能檢視與匯出；請重新建立學習。")

    def view(self, sid: str) -> SessionView:
        row = self.db.get_session(sid)
        if not row:
            raise SessionError("not_found", "找不到學習紀錄")
        content, state = row["content_snapshot"], row["state"]
        q = engine.get_question(content, state.get("question_id"))
        qv = None
        rv = None
        graph = row.get("workflow_runner") == "langgraph" and self.workflow is not None
        wf = self.workflow.workflow_view(row) if graph else None
        if content.get("quotes"):
            rd = state["reading"]
            rv = ReadingView(index=rd["index"], total=len(content["quotes"]), done=rd["done"], quotes=content["quotes"],
                             parts=content["parts"], guides=rd["guides"], qa=rd["qa"],
                             compacts={k: {kk: vv for kk, vv in v.items() if kk != "model_raw"} for k, v in rd["compacts"].items()},
                             quiz_source=rd.get("quiz_source"), segmentation=content.get("segmentation"),
                             stopped_at=rd.get("stopped_at"),
                             job=self.workflow.job_view(wf) if graph else None)
        if q:
            qv = QuestionView(id=q["id"], concept_id=q["concept_id"],
                              concept_title=engine.get_concept(content, q["concept_id"])["title"],
                              type=q["type"], prompt=q["prompt"], hints_used=state["hint_count"],
                              hints_total=int(row["strategy_snapshot"].get("max_hints_before_explain", 2)))
        return SessionView(
            id=row["id"], status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
            content_id=content["id"], content_version=content["version"], content_title=content["title"],
            strategy_id=row["strategy_snapshot"]["id"], strategy_version=row["strategy_snapshot"]["version"],
            source=content.get("source"), reading=rv,
            questions=[{"id": q["id"], "concept_id": q["concept_id"], "type": q["type"], "prompt": q["prompt"],
                        **({"basis": q["basis"]} if q.get("basis") else {})} for q in content["questions"]],
            concepts=[{"id": c["id"], "title": c["title"], "explanation": c["explanation"],
                       "examples": c.get("examples", []),
                       **({"basis": c["basis"], "source_refs": c.get("source_refs", [])} if c.get("basis") else {})}
                      for c in content["concepts"]],
            model_config_snapshot=row["model_config"], state=state, current_question=qv,
            turns=self.db.list_turns(sid), summary=row["summary"],
            workflow_runner=row.get("workflow_runner") or "legacy", revision=row.get("revision") or 0, workflow=wf,
        )

    def list(self) -> list[dict]:
        out = []
        for r in self.db.list_sessions():
            out.append({"id": r["id"], "status": r["status"], "created_at": r["created_at"],
                        "updated_at": r["updated_at"], "content_title": r["content_snapshot"]["title"],
                        "content_version": r["content_snapshot"]["version"],
                        "source": {k: v for k, v in (r["content_snapshot"].get("source") or {}).items() if k != "text"} or None,
                        "strategy_id": r["strategy_snapshot"]["id"],
                        "backend": r["model_config"].get("backend"), "simulated": r["model_config"].get("simulated"),
                        "workflow_runner": r.get("workflow_runner") or "legacy",
                        "verdict": (r["summary"] or {}).get("verdict"),
                        "progress": _progress(r)})
        return out

    # -- one turn -----------------------------------------------------------
    async def answer(self, sid: str, student_answer: str, *, debug_force_invalid: bool = False) -> dict:
        row = self.db.get_session(sid)
        if not row:
            raise SessionError("not_found", "找不到學習紀錄")
        self._legacy_only(row)
        if row["status"] != "active":
            raise SessionError("completed", "本次學習已完成")
        content, strategy, state = row["content_snapshot"], row["strategy_snapshot"], row["state"]
        if state.get("phase") == "reading":
            raise SessionError("reading", "導讀還沒完成，請先讀完所有段落。")
        q = engine.get_question(content, state["question_id"])
        assert q
        concept = engine.get_concept(content, q["concept_id"])
        backend = self.registry.backend
        # The record keeps the config at the time of the turn, not only at session start.
        # Sessions are pinned to the backend they started with; switching mid-session is refused.
        if backend.name != row["model_config"].get("backend"):
            raise SessionError("backend_changed",
                               f"本次學習使用 {row['model_config'].get('backend')}，目前設定為 {backend.name}。請用「重跑」建立新紀錄。")

        messages, meta = self._build_request(content, strategy, state, q, concept, student_answer, sid)
        meta["force_invalid_output"] = debug_force_invalid
        meta["json_schema"] = ModelJudgement.model_json_schema()
        base_turn: dict[str, Any] = {
            "concept_id": q["concept_id"], "question_id": q["id"], "phase": state["phase"],
            "student_answer": student_answer, "model_request": {"messages": messages, "meta": {k: v for k, v in meta.items() if k not in ("force_invalid_output", "json_schema")}},
            "state_before": state, "backend": backend.name, "model": backend.model,
        }

        # Call model; on any failure keep the student's input and surface a retryable error.
        try:
            result = await backend.generate(messages, temperature=self.registry.cfg.temperature,
                                            max_tokens=self.registry.cfg.max_tokens,
                                            context_tokens=self.registry.cfg.context_tokens, meta=meta)
        except LLMError as e:
            turn = self.db.add_turn(sid, {**base_turn, "error": f"connection: {e}"})
            raise SessionError("model_unavailable", str(e), turn=turn) from e

        valid_ids = {c["id"] for c in content["concepts"]}
        try:
            judgement, notes = parse_judgement(result.text, valid_ids)
        except InvalidModelOutput as e:
            # One retry with a stricter nudge, then give up and let the user retry.
            retry_messages = messages + [{"role": "assistant", "content": result.text},
                                         {"role": "user", "content": "你的輸出不是合法 JSON。請只輸出一個 JSON 物件，不要有其他文字。"}]
            try:
                result2 = await backend.generate(retry_messages, temperature=0.0, max_tokens=self.registry.cfg.max_tokens,
                                                 context_tokens=self.registry.cfg.context_tokens, meta=meta)
                judgement, notes = parse_judgement(result2.text, valid_ids)
                notes.insert(0, f"第一次輸出無效（{e}），重試一次後成功")
                result = result2
            except (LLMError, InvalidModelOutput) as e2:
                turn = self.db.add_turn(sid, {**base_turn, "model_raw": result.text, "latency_ms": result.latency_ms,
                                              "usage": result.usage, "error": f"invalid_output: {e2}"})
                raise SessionError("invalid_model_output", f"模型輸出格式無效：{e2}", turn=turn) from e2

        new_state, msg, decision = engine.decide(state, judgement, strategy, content)
        turn = self.db.add_turn(sid, {**base_turn, "model_raw": result.text, "judgement": judgement.model_dump(),
                                      "validation_notes": notes, "decision": decision, "tutor_message": msg.model_dump(),
                                      "state_after": new_state, "latency_ms": result.latency_ms, "usage": result.usage})
        summary = None
        status = "active"
        if new_state["phase"] == "completed":
            status = "completed"
            summary = engine.build_summary(new_state, content, self.db.list_turns(sid))
        self.db.update_session(sid, state=new_state, status=status, summary=summary)
        return {"turn": turn, "message": msg.model_dump(), "session": self.view(sid).model_dump()}

    def _build_request(self, content, strategy, state, q, concept, student_answer, sid):
        turns = self.db.list_turns(sid)
        n = int(strategy.get("recent_turns_in_context", 4))
        recent = [t for t in turns if t.get("question_id") == q["id"] and not t.get("error")][-n:]
        recent_txt = "\n".join(
            f"學生：{t['student_answer']}\n導師：{(t.get('tutor_message') or {}).get('feedback', '')} "
            f"{(t.get('tutor_message') or {}).get('hint') or (t.get('tutor_message') or {}).get('probe') or ''}".strip()
            for t in recent
        ) or "（無）"
        allowed = ["給回饋", "提出一個提示" if state["hint_count"] < strategy.get("max_hints_before_explain", 2) else "（提示次數已用完，系統將顯示教材）",
                   "追問理由"]
        fields = {
            "content_title": content["title"], "concept_title": concept["title"],
            "concept_explanation": concept["explanation"], "question_type": "診斷題" if q["type"] == "diagnostic" else "新情境練習",
            "question_prompt": q["prompt"], "rubric": q["rubric"], "recent_turns": recent_txt,
            "student_answer": student_answer, "allowed_actions": "、".join(allowed),
            "concept_ids": ", ".join(c["id"] for c in content["concepts"]),
        }
        prompt = library.render_prompt(library.load_prompt(strategy.get("prompt_id", "assess")), fields)
        messages = [{"role": "user", "content": prompt}]
        meta = {"student_answer": student_answer, "key_terms": concept.get("key_terms", []), "concept_id": concept["id"],
                "hints": q.get("hints", []), "hint_count": state["hint_count"], "question_id": q["id"]}
        return messages, meta

    # -- replay the same case with current settings ------------------------
    async def replay(self, sid: str) -> dict:
        src = self.db.get_session(sid)
        if not src:
            raise SessionError("not_found", "找不到學習紀錄")
        answers = [t["student_answer"] for t in self.db.list_turns(sid) if not t.get("error")]
        # Same questions as the source record (generated ones exist only in its snapshot); current strategy and model.
        content = src["content_snapshot"]
        if not content["concepts"]:
            raise SessionError("reading", "這筆紀錄還在導讀階段，沒有題目可以重跑。")
        strategy = self.strategies.get(src["strategy_snapshot"]["id"], src["strategy_snapshot"])
        state = engine.initial_state(content)
        if src["state"].get("reading"):
            state["reading"] = src["state"]["reading"]
        new = self.view(self.db.create_session(content, strategy, self.registry.snapshot(), state)["id"])
        replayed, error = 0, None
        for a in answers:
            s = self.db.get_session(new.id)
            if s["status"] != "active":
                break
            try:
                await self.answer(new.id, a)
                replayed += 1
            except SessionError as e:
                error = {"code": e.code, "detail": e.detail}
                break
        return {"source_session_id": sid, "session": self.view(new.id).model_dump(),
                "replayed_answers": replayed, "total_answers": len(answers), "error": error}


def _progress(row: dict) -> dict:
    """Where a session stands, for the home page: reading quote k of n, or past the reading and into the questions."""
    state, total = row["state"] or {}, len(row["content_snapshot"].get("quotes") or [])
    reading = state.get("reading") or {}
    if state.get("phase") == "reading" and total:
        return {"stage": "reading", "read": min(int(reading.get("index") or 0), total), "total": total}
    return {"stage": "quiz", "read": total, "total": total}
