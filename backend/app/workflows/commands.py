"""Events API for LangGraph sessions (plan §7.2, §8): idempotent, validated, persisted before the response.

POST /api/sessions/{sid}/events {command_id, type, expected_revision?, unit_id?, question_id?, payload}
- same session + command_id + same payload → the stored command (200 when finished, 202 while queued / running),
  never rejected because the revision moved on since it was accepted; same id + other payload → 409;
- a new command is validated against where the session waits (DB `workflow_status`, written by the runner)
  and the current unit / question / revision, then stored in a transaction and handed to the runner;
- only one interactive command per session is accepted at a time (409 busy otherwise);
- without a client command_id a server id is generated; that request can't be de-duplicated across retries."""
from __future__ import annotations

import uuid

from ..llm.runtime import stable_hash

EVENT_TYPES = ("guide", "ask", "next", "finish", "answer", "retry")
MAX_TEXT = 4000


class CommandError(Exception):
    def __init__(self, code: str, detail: str, status: int = 409):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def payload_hash(type_: str, unit_id: str | None, question_id: str | None, payload: dict) -> str:
    return stable_hash([type_, unit_id, question_id, payload])


def current_unit(row: dict) -> dict | None:
    st = row["state"]
    if st.get("phase") != "reading":
        return None
    quotes = row["content_snapshot"].get("quotes") or []
    i = st["reading"]["index"]
    return quotes[i] if 0 <= i < len(quotes) else None


def validate(row: dict, type_: str, unit_id: str | None, question_id: str | None, payload: dict,
             expected_revision: int | None, active: list[dict], get_command) -> dict:
    """Raises CommandError; returns the payload to store (a retry carries its target's input)."""
    if type_ not in EVENT_TYPES:
        raise CommandError("bad_event", f"不支援的操作：{type_}", 400)
    if row["status"] == "completed":
        raise CommandError("completed", "本次學習已完成")
    if active:
        raise CommandError("busy", "上一個操作還在處理中，請稍候。")
    if expected_revision is not None and expected_revision != row["revision"]:
        raise CommandError("stale", "學習進度已更新，請重新整理後再試。")
    ws = row.get("workflow_status") or {}
    waiting = ws.get("waiting")
    st = row["state"]
    unit = current_unit(row)
    if unit_id is not None and (unit is None or unit_id not in (unit.get("unit_id"), unit.get("id"))):
        raise CommandError("stale", "目前的段落已經不同，請重新整理後再試。")
    if question_id is not None and question_id != st.get("question_id"):
        raise CommandError("stale", "目前的題目已經不同，請重新整理後再試。")
    shown = unit is not None and str(st["reading"]["index"]) in st["reading"]["guides"]

    def text(key: str) -> str:
        v = (payload.get(key) or "").strip()
        if not v or len(v) > MAX_TEXT:
            raise CommandError("bad_payload", f"請輸入內容（{MAX_TEXT} 字以內）。", 400)
        return v

    if type_ == "guide":
        if waiting != "student":
            raise CommandError("not_ready", "導讀還在準備中。")
        return {}
    if type_ == "ask":
        if waiting != "student" or not shown:
            raise CommandError("not_ready", "請先看完這一段的導讀再發問。")
        return {"question": text("question")}
    if type_ == "next":
        if waiting != "student" or not shown:
            raise CommandError("guide_missing", "請先看完這一段的導讀。")
        return {}
    if type_ == "finish":
        ok = (waiting == "student" and shown) or (waiting == "retry" and ws.get("failed_node") == "guide"
                                                  and st["reading"]["index"] > 0)
        if not ok:
            raise CommandError("guide_missing", "請先看完這一段的導讀，再提前出題。")
        return {}
    if type_ == "answer":
        if waiting != "answer":
            raise CommandError("not_ready" if st.get("phase") == "reading" else "busy", "現在還不能作答。")
        return {"answer": text("answer")}
    # retry
    target_id = payload.get("target_command_id")
    target = get_command(target_id) if target_id else None
    if not target or target["status"] != "failed" or not target["retryable"]:
        raise CommandError("bad_retry", "只能重試失敗且可重試的操作。")
    if waiting == "retry":
        if target_id != ws.get("failed_command_id"):
            raise CommandError("bad_retry", "這個操作已不適用於目前的進度。")
        return {"target_command_id": target_id}
    base, seen = target, set()
    while base and base["type"] == "retry" and base["command_id"] not in seen:   # a retry of a retry → original type
        seen.add(base["command_id"])
        base = get_command(base["payload"].get("target_command_id"))
    base = base or target
    if waiting == "student" and base["type"] == "ask" and target_id == ws.get("last_failed"):
        return {"target_command_id": target_id, "question": target["payload"]["question"]}
    if waiting == "answer" and base["type"] == "answer" and target_id == ws.get("last_failed"):
        return {"target_command_id": target_id, "answer": target["payload"]["answer"]}
    raise CommandError("bad_retry", "這個操作已不適用於目前的進度。")


def new_command_id() -> str:
    return "srv-" + uuid.uuid4().hex
