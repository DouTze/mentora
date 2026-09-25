"""Session orchestration: build model input, validate output, apply rules, persist everything."""
from __future__ import annotations

import asyncio
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
from . import engine, generate, library, reading
from .reading_agent import ReadingAgent, ReadingAgentError, ReadingWorkspace, build_reading_agent


class SessionError(Exception):
    def __init__(self, code: str, detail: str, *, turn: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.turn = turn


class InvalidModelOutput(Exception):
    pass


# Writing questions needs far more room than judging one answer: the whole range goes in, and
# 3 concepts × 2 questions in Chinese come out (a 2048-token cap truncated the JSON mid-object with qwen3-vl:4b).
GEN_MAX_TOKENS = 4096
GEN_CONTEXT_TOKENS = 16384

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
                 reading_agent: ReadingAgent | None = None, reading_dir: Path | None = None):
        self.db = db
        self.registry = registry
        self.materials = materials or MaterialStore(db)
        self.contents = library.load_contents()
        self.strategies = library.load_strategies()
        self.reading_agent = reading_agent or build_reading_agent(registry)
        self.reading_dir = reading_dir or (Path(db.path).parent / "reading" if db.path != ":memory:" else config.READING_DIR)
        self._locks: dict[str, asyncio.Lock] = {}          # serialise state writes per session
        self._guide_tasks: dict[tuple[str, int], asyncio.Task] = {}  # in-flight 導讀 generation (incl. prefetch)
        self._guide_errors: dict[tuple[str, int], dict] = {}          # last failure per quote, until retried
        self._jobs: dict[str, dict] = {}                              # one background job per session
        self._atoms: dict[str, tuple] = {}                            # candidate paragraphs awaiting segmentation

    # -- session lifecycle --------------------------------------------------
    def start(self, content_id: str, strategy_id: str) -> SessionView:
        content = self.contents.get(content_id)
        strategy = self.strategies.get(strategy_id)
        if not content or not strategy:
            raise SessionError("not_found", "找不到教材或教學策略")
        row = self.db.create_session(content, strategy, self.registry.snapshot(), engine.initial_state(content))
        return self.view(row["id"])

    async def start_from_material(self, material_id: str, start: int, end: int, strategy_id: str) -> SessionView:
        """Open the session in the 導讀 phase right away. The rule-based candidate paragraphs stand in as quotes
        until the model has grouped them into 來源單元 (background job "segment"); the first 導讀 waits for that."""
        strategy = self.strategies.get(strategy_id)
        if not strategy:
            raise SessionError("not_found", "找不到教學策略")
        material, text = self.materials.range_text(material_id, start, end)
        _, units = self.materials.range_units(material_id, start, end)
        atoms = reading.split_atoms(units, material["unit"], start)
        if not atoms:
            raise SessionError("no_text", "這個範圍裡沒有可導讀的文字。")
        quotes = reading.finish_quotes(material["unit"], atoms)
        content = generate.empty_content(material, start, end, text)
        content["quotes"], content["parts"] = quotes, reading.plan_parts(quotes)
        planner = config.QUOTE_PLANNER
        content["segmentation"] = {"planner": planner, "candidates": len(atoms), "notes": [],
                                   "status": "pending" if planner == "model" else "done"}
        row = self.db.create_session(content, strategy, self.registry.snapshot(), reading.initial_state(content))
        sid = row["id"]
        ReadingWorkspace(self.reading_dir, sid).init(content)
        if planner == "model":
            self._atoms[sid] = (material["unit"], material["title"], atoms)
            self._start_job(sid, "segment", lambda: self._segment_job(sid))
        return self.view(sid)

    async def _segment_job(self, sid: str) -> None:
        unit, title, atoms = self._atoms.pop(sid, (None, None, None))
        notes: list[str] = []
        if atoms is None:  # server restarted while pending: keep the paragraph split
            groups, notes = None, ["伺服器重新啟動，沿用段落切法"]
        else:
            groups = []
            for a, b in reading.plan_segments(atoms):
                chunk = atoms[a:b]
                try:
                    groups += reading.apply_segmentation(chunk, await self.reading_agent.segment(title, chunk))
                except ReadingAgentError as e:
                    if e.code == "model_unavailable":
                        self._atoms[sid] = (unit, title, atoms)  # keep the candidates so a retry can re-run
                        raise
                    notes.append(f"候選段落 {a + 1}–{b} 改用規則切法：{e.detail}")
                    groups += chunk
        async with self._lock(sid):
            row = self.db.get_session(sid)
            assert row
            content = row["content_snapshot"]
            if groups is not None:
                content["quotes"] = reading.finish_quotes(unit, groups)
                content["parts"] = reading.plan_parts(content["quotes"])
            content["segmentation"].update({"notes": notes, "status": "done"})
            self.db.update_content(sid, content)
            ws = ReadingWorkspace(self.reading_dir, sid)
            ws.clear_quotes()
            ws.init(content)
        # the student is waiting on the first quote: start its 導讀 right away instead of on the next request
        self._prefetch(sid, 0, len(content["quotes"]))

    async def _write_questions(self, content: dict, state: dict) -> dict:
        """Ask the model for concepts + questions once the reading is done; from the compact notes when the
        range is too long to send whole."""
        material = {"id": content["source"]["material_id"], "title": content["source"]["material_title"],
                    "unit": content["source"]["unit"]}
        src = content["source"]
        upto = state["reading"].get("stopped_at")
        read = content["quotes"] if upto is None else content["quotes"][:upto + 1]
        read_text = "\n\n".join(q["text"] for q in read)
        read_parts = [p for p in content["parts"] if p["quote_from"] <= read[-1]["index"]]
        text, mode = reading.quiz_source_text(read_text, read_parts, state["reading"]["compacts"])
        messages = generate.build_messages(material, src["start"], src["end"], text, from_notes=mode == "compact",
                                           reading_log=reading.reading_log(read, state["reading"]))
        cfg, backend = self.registry.cfg, self.registry.backend
        last_error: Exception | None = None
        for attempt in range(2):  # one retry, same as answer judging
            try:
                result = await backend.generate(messages, temperature=0.0 if attempt else cfg.temperature,
                                                max_tokens=max(cfg.max_tokens, GEN_MAX_TOKENS), context_tokens=max(cfg.context_tokens, GEN_CONTEXT_TOKENS),
                                                meta={"task": "generate", "range_text": text,
                                                      "reading_questions": [x["question"] for qs in state["reading"]["qa"].values() for x in qs],
                                                      "json_schema": generate.Generated.model_json_schema()})
                gen = generate.parse(result.text)
                break
            except LLMError as e:
                raise SessionError("model_unavailable", str(e)) from e
            except generate.InvalidGeneration as e:
                last_error = e
        else:
            raise SessionError("invalid_model_output", f"模型沒有產生可用的題目：{last_error}")
        state["reading"]["quiz_source"] = mode
        return generate.fill_content(content, gen)

    # -- 導讀 ------------------------------------------------------------------
    # Model calls take minutes on a small local model, longer than any HTTP proxy will wait. So every request
    # here returns at once; the work runs as a background job on the server's event loop, its stage/error is
    # exposed in the session view (reading.job), and the page polls the session until the job is gone.
    def _lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    def _start_job(self, sid: str, stage: str, run) -> bool:
        """One background job per session. Returns False when one is still running."""
        cur = self._jobs.get(sid)
        if cur and not cur["task"].done():
            return False
        job: dict[str, Any] = {"stage": stage, "error": None, "task": None}

        async def wrapped() -> None:
            try:
                await run()
            except ReadingAgentError as e:
                job["error"] = {"code": e.code, "detail": e.detail}
            except SessionError as e:
                job["error"] = {"code": e.code, "detail": e.detail}
            except Exception as e:  # noqa: BLE001 — a job must never die silently
                job["error"] = {"code": "internal", "detail": f"{type(e).__name__}: {e}"}

        job["task"] = asyncio.ensure_future(wrapped())
        self._jobs[sid] = job
        return True

    def _job_view(self, sid: str, state: dict) -> dict | None:
        """What the page shows while it waits: {"stage", "error"}; None when nothing is running or failed."""
        job = self._jobs.get(sid)
        if job:
            if not job["task"].done():
                return {"stage": job["stage"], "error": None}
            if job["error"]:
                return {"stage": job["stage"], "error": job["error"]}
        rd = state.get("reading")
        if rd and not rd["done"]:
            key = (sid, rd["index"])
            if key in self._guide_tasks and not self._guide_tasks[key].done():
                return {"stage": "guide", "error": None}
            err = self._guide_errors.get(key)
            if err:
                return {"stage": "guide", "error": err}
        return None

    def _reading_row(self, sid: str) -> dict:
        row = self.db.get_session(sid)
        if not row:
            raise SessionError("not_found", "找不到學習紀錄")
        if row["state"].get("phase") != "reading":
            raise SessionError("not_reading", "本次學習已完成導讀，進入出題階段。")
        return row

    def _prev_points(self, content: dict, state: dict, index: int) -> list[str]:
        pts: list[str] = []
        for i in range(max(0, index - 3), index):
            g = state["reading"]["guides"].get(str(i))
            if g:
                pts += g.get("key_points", [])
        return pts[-6:]

    def _guide_task(self, sid: str, index: int) -> asyncio.Task:
        """The (possibly already running) generation of the 導讀 for quote `index`."""
        key = (sid, index)
        task = self._guide_tasks.get(key)
        if task is None or (task.done() and task.exception() is not None and key not in self._guide_errors):
            task = asyncio.ensure_future(self._generate_guide(sid, index))
            self._guide_tasks[key] = task
        return task

    async def _generate_guide(self, sid: str, index: int) -> dict:
        key = (sid, index)
        self._guide_errors.pop(key, None)
        try:
            row = self.db.get_session(sid)
            assert row
            content, state = row["content_snapshot"], row["state"]
            quote = content["quotes"][index]
            ws = ReadingWorkspace(self.reading_dir, sid)
            guide = await self.reading_agent.guide(ws, content, quote, self._prev_points(content, state, index))
            async with self._lock(sid):
                fresh = self.db.get_session(sid)
                assert fresh
                fresh["state"]["reading"]["guides"][str(index)] = guide
                self.db.update_state(sid, fresh["state"])
            return guide
        except ReadingAgentError as e:
            self._guide_errors[key] = {"code": e.code, "detail": e.detail}
            raise
        except Exception as e:  # noqa: BLE001
            self._guide_errors[key] = {"code": "internal", "detail": f"{type(e).__name__}: {e}"}
            raise
        finally:
            self._guide_tasks.pop(key, None)

    def _prefetch(self, sid: str, index: int, total: int) -> None:
        if config.READING_PREFETCH and index < total and (sid, index) not in self._guide_tasks:
            self._guide_task(sid, index)

    async def reading_guide(self, sid: str) -> dict:
        """導讀 for the current quote. Cached → returned; otherwise its generation is started (or is already
        running) and the page polls the session. A failed attempt is retried by calling this again."""
        row = self._reading_row(sid)
        content, rd = row["content_snapshot"], row["state"]["reading"]
        if content.get("segmentation", {}).get("status") == "pending":
            job = self._jobs.get(sid)
            if not job or job["task"].done():   # lost (restart) or failed → run it again
                self._start_job(sid, "segment", lambda: self._segment_job(sid))
            return {"status": "pending", "guide": None, "session": self.view(sid).model_dump()}
        cached = rd["guides"].get(str(rd["index"]))
        if cached:
            self._prefetch(sid, rd["index"] + 1, len(content["quotes"]))
            return {"status": "ready", "guide": cached, "session": self.view(sid).model_dump()}
        key = (sid, rd["index"])
        if key in self._guide_errors and key not in self._guide_tasks:
            self._guide_errors.pop(key)   # explicit retry
        self._guide_task(sid, rd["index"])
        return {"status": "pending", "guide": None, "session": self.view(sid).model_dump()}

    async def reading_ask(self, sid: str, question: str) -> dict:
        row = self._reading_row(sid)
        content, rd = row["content_snapshot"], row["state"]["reading"]
        idx = rd["index"]
        quote = content["quotes"][idx]
        history = reading.ask_history(content["quotes"], rd, idx)
        try:
            qa = await self.reading_agent.ask(ReadingWorkspace(self.reading_dir, sid), content, quote,
                                              rd["guides"].get(str(idx)), history, question)
        except ReadingAgentError as e:
            raise SessionError(e.code, e.detail) from e
        async with self._lock(sid):
            fresh = self.db.get_session(sid)
            assert fresh
            fresh["state"]["reading"]["qa"].setdefault(str(idx), []).append(qa)
            self.db.update_state(sid, fresh["state"])
        return {"answer": qa, "session": self.view(sid).model_dump()}

    async def _compact_part(self, sid: str, content: dict, part: dict, upto: int | None = None) -> None:
        """Compact a part once it has been read (only its read quotes when the student stopped early)."""
        p = dict(part)
        if upto is not None and upto < part["quote_to"]:
            p["quote_to"] = upto
            p["label"] += f"（讀到第 {upto + 1} 段）"
        compact = await self.reading_agent.compact(ReadingWorkspace(self.reading_dir, sid), content, p)
        async with self._lock(sid):
            fresh = self.db.get_session(sid)
            assert fresh
            fresh["state"]["reading"]["compacts"][str(part["index"])] = compact
            self.db.update_state(sid, fresh["state"])

    async def _write_quiz(self, sid: str) -> None:
        row = self.db.get_session(sid)
        assert row
        content, state = row["content_snapshot"], row["state"]
        filled = await self._write_questions(content, state)
        new_state = engine.initial_state(filled)
        new_state["reading"] = state["reading"]
        async with self._lock(sid):
            self.db.update_content(sid, filled)
            self.db.update_state(sid, new_state)

    async def _advance_job(self, sid: str, idx: int, part: dict | None, finished: bool) -> None:
        row = self.db.get_session(sid)
        assert row
        content = row["content_snapshot"]
        if part is not None:
            self._jobs[sid]["stage"] = "compact"
            await self._compact_part(sid, content, part)
        async with self._lock(sid):
            fresh = self.db.get_session(sid)
            assert fresh
            st = fresh["state"]
            st["reading"]["index"] = min(idx + 1, len(content["quotes"]) - 1)
            if finished:
                st["reading"]["done"] = True
            self.db.update_state(sid, st)
        if finished:
            self._jobs[sid]["stage"] = "generate"
            await self._write_quiz(sid)
        else:
            self._prefetch(sid, idx + 2, len(content["quotes"]))

    async def reading_next(self, sid: str) -> dict:
        """The student understood the current quote. Moving on is instant; finishing a part (compact) or the
        whole range (write the questions) runs as a job."""
        row = self._reading_row(sid)
        content, state = row["content_snapshot"], row["state"]
        rd = state["reading"]
        idx = rd["index"]
        if str(idx) not in rd["guides"]:
            raise SessionError("guide_missing", "請先看完這一段的導讀。")
        job = self._jobs.get(sid)
        if job and not job["task"].done():
            return {"status": "pending", "session": self.view(sid).model_dump()}
        quote = content["quotes"][idx]
        part = content["parts"][quote["part"]]
        needs_compact = idx == part["quote_to"] and str(part["index"]) not in rd["compacts"] and len(content["parts"]) > 1
        finished = idx + 1 >= len(content["quotes"])
        if not needs_compact and not finished:
            async with self._lock(sid):
                fresh = self.db.get_session(sid)
                assert fresh
                fresh["state"]["reading"]["index"] = idx + 1
                self.db.update_state(sid, fresh["state"])
            self._prefetch(sid, idx + 2, len(content["quotes"]))
            return {"status": "ready", "session": self.view(sid).model_dump()}
        self._start_job(sid, "compact" if needs_compact else "generate",
                        lambda: self._advance_job(sid, idx, part if needs_compact else None, finished))
        return {"status": "pending", "session": self.view(sid).model_dump()}

    async def _finish_job(self, sid: str, upto: int) -> None:
        row = self.db.get_session(sid)
        assert row
        content, rd = row["content_snapshot"], row["state"]["reading"]
        last_part = content["quotes"][upto]["part"]
        if len(content["parts"]) > 1:
            self._jobs[sid]["stage"] = "compact"
            for part in content["parts"][:last_part + 1]:
                if str(part["index"]) not in rd["compacts"]:
                    await self._compact_part(sid, content, part, upto)
        async with self._lock(sid):
            fresh = self.db.get_session(sid)
            assert fresh
            fresh["state"]["reading"].update({"done": True, "stopped_at": upto})
            self.db.update_state(sid, fresh["state"])
        self._jobs[sid]["stage"] = "generate"
        await self._write_quiz(sid)

    async def reading_finish(self, sid: str) -> dict:
        """Stop reading here and go to the questions: they are written from the quotes read so far."""
        row = self._reading_row(sid)
        rd = row["state"]["reading"]
        idx = rd["index"]
        if str(idx) not in rd["guides"]:
            raise SessionError("guide_missing", "請先看完這一段的導讀，再提前出題。")
        self._start_job(sid, "generate", lambda: self._finish_job(sid, idx))
        return {"status": "pending", "session": self.view(sid).model_dump()}

    def view(self, sid: str) -> SessionView:
        row = self.db.get_session(sid)
        if not row:
            raise SessionError("not_found", "找不到學習紀錄")
        content, state = row["content_snapshot"], row["state"]
        q = engine.get_question(content, state.get("question_id"))
        qv = None
        rv = None
        if content.get("quotes"):
            rd = state["reading"]
            rv = ReadingView(index=rd["index"], total=len(content["quotes"]), done=rd["done"], quotes=content["quotes"],
                             parts=content["parts"], guides=rd["guides"], qa=rd["qa"],
                             compacts={k: {kk: vv for kk, vv in v.items() if kk != "model_raw"} for k, v in rd["compacts"].items()},
                             quiz_source=rd.get("quiz_source"), segmentation=content.get("segmentation"),
                             stopped_at=rd.get("stopped_at"), job=self._job_view(sid, state))
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
            questions=[{"id": q["id"], "concept_id": q["concept_id"], "type": q["type"], "prompt": q["prompt"]} for q in content["questions"]],
            concepts=[{"id": c["id"], "title": c["title"], "explanation": c["explanation"],
                                                     "examples": c.get("examples", [])} for c in content["concepts"]],
            model_config_snapshot=row["model_config"], state=state, current_question=qv,
            turns=self.db.list_turns(sid), summary=row["summary"],
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
                        "verdict": (r["summary"] or {}).get("verdict")})
        return out

    # -- one turn -----------------------------------------------------------
    async def answer(self, sid: str, student_answer: str, *, debug_force_invalid: bool = False) -> dict:
        row = self.db.get_session(sid)
        if not row:
            raise SessionError("not_found", "找不到學習紀錄")
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
