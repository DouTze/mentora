"""Durable learning flow (P4): events API, idempotent commands, serial sessions, retries, and restarts at every
fault window — business commit before checkpoint, resume before command completion, inside the reading subgraph,
between planning windows."""
import io
import re
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import child_env
from fastapi.testclient import TestClient

from app import config
from app.db import Database
from app.llm.base import LLMError
from app.llm.mock import MockBackend
from app.llm.registry import Registry
from app.main import create_app
from app.models import ModelConfig

HELPERS = Path(__file__).parent / "helpers"
MD = (HELPERS / "flow.md").read_text(encoding="utf-8")


@pytest.fixture
def graph_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "READING_PREFETCH", False)   # call counts below are per command; prefetch has its own tests
    app = create_app(Database(tmp_path / "db.sqlite3"), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def idle(c, sid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/sessions/{sid}").json()
        if not s["workflow"]["active_command"]:
            return s
        time.sleep(0.01)
    raise AssertionError("runner did not go idle")


def start(c, text=MD):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(text.encode()))}).json()
    s = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": len(text.splitlines())}).json()
    assert s["workflow_runner"] == "langgraph"
    return idle(c, s["id"])


def event(c, sid, type_, cid, expect=(200, 202), **kw):
    r = c.post(f"/api/sessions/{sid}/events", json={"command_id": cid, "type": type_, **kw})
    assert r.status_code in (expect if isinstance(expect, tuple) else (expect,)), r.text
    return r


def run(c, sid, type_, cid, **kw):
    event(c, sid, type_, cid, **kw)
    s = idle(c, sid)
    return s, c.get(f"/api/sessions/{sid}/commands/{cid}").json()


# -- full flow -------------------------------------------------------------------------

def test_full_flow_through_events_api(graph_app):
    c = graph_app
    s = start(c)
    assert s["reading"]["segmentation"]["status"] == "done" and s["workflow"]["waiting"] == "student"
    assert "0" in s["reading"]["guides"] and s["reading"]["guides"]["0"]["review_status"] == "passed"
    assert s["reading"]["quotes"][0]["unit_id"] and s["reading"]["quotes"][0]["block_ids"]
    rev = s["revision"]
    s, cmd = run(c, s["id"], "ask", "q1", payload={"question": "葉綠體是什麼？"})
    assert cmd["status"] == "completed" and "葉綠體" in cmd["result"]["answer"]["answer"]
    assert s["reading"]["qa"]["0"][0]["command_id"] == "q1" and s["revision"] == rev + 1
    for i in range(3):
        s, cmd = run(c, s["id"], "next", f"n{i}", unit_id=s["reading"]["quotes"][s["reading"]["index"]]["unit_id"],
                     expected_revision=s["revision"])
        assert cmd["status"] == "completed", cmd
    assert s["state"]["phase"] == "diagnostic" and s["workflow"]["waiting"] == "answer" and s["concepts"]
    assert s["state"]["reading"]["quiz_scope"] and s["state"]["reading"]["read"] == [0, 1, 2]
    k = 0
    while s["status"] != "completed":
        s, cmd = run(c, s["id"], "answer", f"a{k}", payload={"answer": "因為有共同的第三個因素影響兩者，例如光照與二氧化碳"},
                     question_id=s["current_question"]["id"])
        assert cmd["status"] == "completed", cmd
        k += 1
        assert k < 20
    assert s["summary"] and len(s["turns"]) == k and all(t["command_id"] for t in s["turns"])
    md = c.get(f"/api/sessions/{s['id']}/export?format=md")
    assert md.status_code == 200 and "## 導讀" in md.text and "- 流程：langgraph" in md.text and "_檢查：passed" in md.text
    rp = c.post(f"/api/sessions/{s['id']}/replay").json()   # replay = a new record with current settings
    assert rp["replayed_answers"] == k and rp["error"] is None and rp["session"]["workflow_runner"] == "legacy"
    trace = c.get(f"/api/sessions/{s['id']}/trace").json()
    assert {x["node"] for x in trace["llm_calls"]} >= {"understand", "plan_bridges", "draft", "body_audit", "ask.answer", "quiz_generate", "quiz_audit", "assess"}
    # every task stayed inside its budget
    from app.llm.runtime import BUDGETS
    by_op: dict = {}
    for x in trace["llm_calls"]:   # a guide attempt's nodes share one budget (op prefix up to the workflow version)
        key = x["operation_key"].split(":reading-graph-v2")[0] if x["task"] == "guide" else x["operation_key"]
        by_op[(x["task"], key)] = by_op.get((x["task"], key), 0) + 1
    assert all(n <= BUDGETS[task]["max_calls"] for (task, _), n in by_op.items()), by_op


def test_reading_sessions_run_only_through_events_and_old_flow_sessions_are_read_only(graph_app):
    """Every material session uses the reading workflow; the endpoints of the removed old flow are gone, and a
    session the old flow left in the database can still be viewed and exported but not continued."""
    c = graph_app
    s = start(c)
    for path in ("reading/guide", "reading/next", "reading/finish"):
        assert c.post(f"/api/sessions/{s['id']}/{path}").status_code in (404, 405)
    assert c.post(f"/api/sessions/{s['id']}/answer", json={"answer": "x"}).json()["code"] == "use_events"
    db = c.app_ref.state.db
    row = db.get_session(s["id"])
    old = db.create_session(row["content_snapshot"], row["strategy_snapshot"], row["model_config"], row["state"])
    view = c.get(f"/api/sessions/{old['id']}").json()
    assert view["workflow_runner"] == "legacy" and view["workflow"] is None and view["reading"]["job"] is None
    r = c.post(f"/api/sessions/{old['id']}/events", json={"command_id": "x", "type": "next"})
    assert r.status_code == 409 and r.json()["code"] == "legacy_session"
    r = c.post(f"/api/sessions/{old['id']}/answer", json={"answer": "x"})
    assert r.status_code == 409 and r.json()["code"] == "legacy_readonly"
    assert c.get(f"/api/sessions/{old['id']}/export", params={"format": "md"}).status_code == 200


# -- idempotency / concurrency ---------------------------------------------------------------

def test_resent_command_is_applied_once_and_conflicting_reuse_is_rejected(graph_app):
    c = graph_app
    s = start(c)
    run(c, s["id"], "ask", "q1", payload={"question": "葉綠體是什麼？"})
    r = event(c, s["id"], "ask", "q1", payload={"question": "葉綠體是什麼？"}, expect=200)   # network resend
    assert r.json()["status"] == "completed"
    s2 = idle(c, s["id"])
    assert len(s2["reading"]["qa"]["0"]) == 1 and s2["revision"] == s["revision"] + 1
    r = event(c, s["id"], "ask", "q1", payload={"question": "別的問題"}, expect=409)
    assert r.json()["code"] == "command_conflict"
    # an old command resent after the revision moved on is still answered, not rejected as stale
    run(c, s["id"], "next", "n1")
    assert event(c, s["id"], "ask", "q1", payload={"question": "葉綠體是什麼？"}, expect=200).json()["status"] == "completed"


def test_one_interactive_command_at_a_time(graph_app):
    c = graph_app
    s = start(c)
    runner = c.app_ref.state.runner
    runner.active_backup, runner.notify = runner.notify, lambda sid: None   # hold the queue
    event(c, s["id"], "next", "n1", expect=202)
    r1 = event(c, s["id"], "next", "n2", expect=409)       # double next
    r2 = event(c, s["id"], "ask", "q1", payload={"question": "?"}, expect=409)   # ask racing next
    assert r1.json()["code"] == r2.json()["code"] == "busy" and r1.json()["session"]["id"] == s["id"]
    runner.notify = runner.active_backup

    async def kick():
        runner.notify(s["id"])
    c.portal.call(kick)
    s2 = idle(c, s["id"])
    assert s2["reading"]["index"] == 1 and s2["state"]["reading"]["read"] == [0]


def test_stale_revision_or_unit_is_refused_with_the_latest_session(graph_app):
    c = graph_app
    s = start(c)
    r = event(c, s["id"], "next", "n1", expected_revision=s["revision"] - 1, expect=409)
    assert r.json()["code"] == "stale" and r.json()["session"]["revision"] == s["revision"]
    r = event(c, s["id"], "next", "n2", unit_id=s["reading"]["quotes"][1]["unit_id"], expect=409)
    assert r.json()["code"] == "stale"
    r = event(c, s["id"], "answer", "a1", payload={"answer": "x"}, expect=409)
    assert r.json()["code"] == "not_ready"


# -- failures and retry ------------------------------------------------------------------------

def test_guide_failure_waits_for_retry_and_retry_resumes(graph_app, monkeypatch):
    c = graph_app
    real = MockBackend.generate
    state = {"down": True}

    async def flaky(self, messages, **kw):
        if state["down"] and (kw.get("meta") or {}).get("step") == "draft":
            raise LLMError("Ollama 連線失敗：refused")
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "generate", flaky)
    s = start(c)
    wf = s["workflow"]
    assert wf["waiting"] == "retry" and wf["error"]["code"] == "model_unavailable" and wf["failed_node"] == "guide"
    assert s["reading"]["job"]["error"]["code"] == "model_unavailable"   # legacy page shape still shows it
    start_cmd = c.get(f"/api/sessions/{s['id']}/commands/start-{s['id']}").json()
    assert start_cmd["status"] == "failed" and start_cmd["retryable"]
    assert event(c, s["id"], "next", "n1", expect=409).json()["code"] == "guide_missing"
    state["down"] = False
    s, cmd = run(c, s["id"], "retry", "r1", payload={"target_command_id": f"start-{s['id']}"})
    assert cmd["status"] == "completed" and "0" in s["reading"]["guides"] and s["workflow"]["waiting"] == "student"
    trace = c.get(f"/api/sessions/{s['id']}/trace").json()["llm_calls"]
    assert [x["node"] for x in trace].count("understand") == 1   # resumed at the failed node


def test_failed_ask_keeps_reading_open_and_can_be_retried(graph_app, monkeypatch):
    c = graph_app
    s = start(c)
    real = MockBackend.generate
    state = {"down": True}

    async def flaky(self, messages, **kw):
        if state["down"] and (kw.get("meta") or {}).get("task") == "ask":
            raise LLMError("down")
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "generate", flaky)
    s, cmd = run(c, s["id"], "ask", "q1", payload={"question": "ATP 是什麼？"})
    assert cmd["status"] == "failed" and cmd["retryable"] and s["workflow"]["waiting"] == "student"
    assert s["reading"]["qa"] == {}
    state["down"] = False
    s, cmd = run(c, s["id"], "retry", "r1", payload={"target_command_id": "q1"})
    assert cmd["status"] == "completed" and s["reading"]["qa"]["0"][0]["question"] == "ATP 是什麼？"


def test_finish_early_freezes_the_read_scope(graph_app):
    c = graph_app
    s = start(c)
    run(c, s["id"], "next", "n1")
    s, cmd = run(c, s["id"], "finish", "f1")
    assert cmd["status"] == "completed" and s["state"]["phase"] == "diagnostic"
    rd = s["state"]["reading"]
    assert rd["stopped_at"] == 1 and len(rd["quiz_scope"]) == 2 and "暗反應" not in json.dumps(s["concepts"], ensure_ascii=False)


def test_generate_failure_keeps_preparing_and_retry_uses_same_scope(graph_app, monkeypatch):
    c = graph_app
    s = start(c)
    real = MockBackend.generate
    state = {"bad": True}

    async def flaky(self, messages, **kw):
        if state["bad"] and (kw.get("meta") or {}).get("task") == "generate":
            from app.llm.base import LLMResult
            return LLMResult(text="不是 JSON", latency_ms=1)
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "generate", flaky)
    run(c, s["id"], "next", "n1")
    s, cmd = run(c, s["id"], "finish", "f1")
    assert cmd["status"] == "failed" and s["state"]["phase"] == "reading" and s["workflow"]["failed_node"] == "generate"
    assert s["state"]["reading"]["stopped_at"] == 1
    state["bad"] = False
    s, cmd = run(c, s["id"], "retry", "r1", payload={"target_command_id": "f1"})
    assert cmd["status"] == "completed" and s["state"]["phase"] == "diagnostic" and s["state"]["reading"]["stopped_at"] == 1
    assert s["state"]["reading"]["read"] == [0, 1]


# -- restarts (fresh processes, file SQLite) ------------------------------------------------------------

def proc(tmp_path, steps, fault=None):
    env = child_env(HOME=str(tmp_path), **({"MENTOR_FAULT": fault} if fault else {}))
    p = subprocess.run([sys.executable, str(HELPERS / "workflow_proc.py"), str(tmp_path), json.dumps(steps)],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    if fault:
        assert p.returncode == 77, (p.returncode, p.stderr[-2000:])
        return None
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


START = [{"op": "start"}, {"op": "wait"}]
WAIT = [{"op": "wait"}]


@pytest.mark.parametrize("fault", ["after_commit:ask", "before_complete"])
def test_restart_after_ask_commit_does_not_duplicate_the_answer(tmp_path, fault):
    proc(tmp_path, START)
    proc(tmp_path, [{"op": "event", "type": "ask", "command_id": "q1", "payload": {"question": "葉綠體是什麼？"}}, {"op": "wait"}],
         fault=fault)
    out = proc(tmp_path, WAIT)
    assert out["qa"] == {"0": 1} and out["commands"]["q1"] == "completed" and out["waiting"] == "student"
    assert [n for n, _ in out["calls"]].count("ask.answer") == 1


@pytest.mark.parametrize("fault", ["after_commit:next", "after_commit:guide", "before_complete"])
def test_restart_during_next_advances_exactly_once(tmp_path, fault):
    proc(tmp_path, START)
    before = proc(tmp_path, WAIT)
    proc(tmp_path, [{"op": "event", "type": "next", "command_id": "n1"}, {"op": "wait"}], fault=fault)
    out = proc(tmp_path, WAIT)
    assert out["index"] == 1 and out["read"] == [0] and out["guides"] == ["0", "1"]
    assert out["commands"]["n1"] == "completed" and out["waiting"] == "student"
    # one read commit + one compact commit (unit 0 ends its section, so its part is compacted) + one guide commit,
    # never twice
    assert out["revision"] == before["revision"] + 3


def test_restart_before_completion_does_not_resume_the_event_again(tmp_path):
    """Graph already reached the next wait point; only the completion mark was lost."""
    proc(tmp_path, START)
    proc(tmp_path, [{"op": "event", "type": "next", "command_id": "n1"}, {"op": "wait"}], fault="before_complete")
    out = proc(tmp_path, WAIT)
    assert out["index"] == 1 and out["commands"]["n1"] == "completed"
    out2 = proc(tmp_path, [{"op": "event", "type": "next", "command_id": "n2"}, {"op": "wait"}])
    assert out2["index"] == 2 and out2["read"] == [0, 1]


def test_restart_during_segmentation_and_guide_reuses_finished_work(tmp_path):
    proc(tmp_path, [{"op": "start"}, {"op": "wait"}], fault="after_commit:segment")
    out = proc(tmp_path, WAIT)
    assert out["segmentation"] == "done" and out["guides"] == ["0"] and out["commands"][f"start-{out['sid']}"] == "completed"
    names = [n for n, _ in out["calls"]]
    assert names.count("segment.o01") == 1 and names.count("segment.t01") == 1   # outlines and topics not redone


def test_restart_after_quiz_commit_and_after_assess_commit(tmp_path):
    proc(tmp_path, START)
    proc(tmp_path, [{"op": "event", "type": "finish", "command_id": "f1"}, {"op": "wait"}], fault="after_commit:generate")
    out = proc(tmp_path, WAIT)
    assert out["phase"] == "diagnostic" and out["commands"]["f1"] == "completed"
    assert [n for n, _ in out["calls"]].count("quiz_generate") == 1
    proc(tmp_path, [{"op": "event", "type": "answer", "command_id": "a1", "payload": {"answer": "因為有第三個因素同時影響兩者"}},
                    {"op": "wait"}], fault="after_commit:assess")
    out = proc(tmp_path, WAIT)
    assert out["turns"] == 1 and out["commands"]["a1"] == "completed"
    assert [n for n, _ in out["calls"]].count("assess") == 1


def test_second_runner_on_the_same_database_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    a = create_app(Database(db), Registry(ModelConfig(backend="mock")))
    b = create_app(Database(db), Registry(ModelConfig(backend="mock")))
    with TestClient(a) as ca, TestClient(b) as cb:
        assert a.state.runner.active and not b.state.runner.active
        s = start(ca)
        r = cb.post(f"/api/sessions/{s['id']}/events", json={"command_id": "x", "type": "next"})
        assert r.status_code == 503 and r.json()["code"] == "runner_unavailable"
        assert cb.get(f"/api/sessions/{s['id']}").status_code == 200   # still readable


# -- prefetch -------------------------------------------------------------------------------------

def prefetch_idle(c, sid):
    s = idle(c, sid)
    c.portal.call(c.app_ref.state.runner.idle)
    return s


def test_prefetched_guide_is_adopted_without_model_calls(graph_app, monkeypatch):
    c = graph_app
    monkeypatch.setattr(config, "READING_PREFETCH", True)
    s = start(c)
    prefetch_idle(c, s["id"])
    log = c.app_ref.state.runner.prefetch_log
    assert (s["id"], "written") in log
    before = len(c.get(f"/api/sessions/{s['id']}/trace").json()["llm_calls"])
    s, cmd = run(c, s["id"], "next", "n1")
    trace = c.get(f"/api/sessions/{s['id']}/trace").json()["llm_calls"]
    assert cmd["status"] == "completed" and s["reading"]["guides"]["1"]["prefetched"] is True
    # the next guide cost nothing (the part that ended with unit 0 is compacted on this command, as before)
    assert not [x for x in trace[before:] if x["command_id"] == "n1" and x["node"] not in ("notes", "notes_audit", "notes_revise")]
    assert s["state"]["reading"]["read"] == [0] and s["reading"]["index"] == 1  # prefetch never moved progress


def test_a_question_after_prefetch_invalidates_the_cached_guide(graph_app, monkeypatch):
    c = graph_app
    monkeypatch.setattr(config, "READING_PREFETCH", True)
    s = start(c)
    prefetch_idle(c, s["id"])
    run(c, s["id"], "ask", "q1", payload={"question": "葉綠體是什麼？"})
    prefetch_idle(c, s["id"])
    s, cmd = run(c, s["id"], "next", "n1")
    g = s["reading"]["guides"]["1"]
    assert cmd["status"] == "completed"
    # the guide reflects the Q&A: either a fresh prefetch made after the question, or written on demand by n1
    calls = c.get(f"/api/sessions/{s['id']}/trace").json()["llm_calls"]
    unit1 = s["reading"]["quotes"][1]["unit_id"]   # the runner may already be prefetching unit 3 by now: ignore it
    # a 主題 is prefetched one 區塊 at a time; each 區塊's operation key carries the reader's context
    runs = {m.group(1) for x in calls if x["command_id"] == "prefetch"
            for m in [re.search(rf":prefetch:{unit1}-b\d+:a\d+:(\w+):", x["operation_key"])] if m}
    assert len(runs) == 2   # before / after the question
    assert g.get("prefetched") in (True, None)


def test_a_command_cancels_a_running_prefetch_and_still_completes(graph_app, monkeypatch):
    import asyncio
    c = graph_app
    monkeypatch.setattr(config, "READING_PREFETCH", True)
    real = MockBackend.generate

    async def slow(self, messages, **kw):
        if (kw.get("meta") or {}).get("step") == "understand":
            await asyncio.sleep(0.3)
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "generate", slow)
    s = start(c)
    s, cmd = run(c, s["id"], "ask", "q1", payload={"question": "ATP 是什麼？"})   # arrives while prefetch runs
    prefetch_idle(c, s["id"])
    assert cmd["status"] == "completed" and len(s["reading"]["qa"]["0"]) == 1
    assert (s["id"], "cancelled") in c.app_ref.state.runner.prefetch_log


def test_next_during_a_prefetch_waits_for_it_and_adopts_the_guide(graph_app, monkeypatch):
    """R8: at a normal reading pace the reader presses next while the next guide is still being prefetched. The
    prefetch is kept, finishes, and its guide is adopted — the work is not thrown away and redone."""
    import asyncio
    c = graph_app
    monkeypatch.setattr(config, "READING_PREFETCH", True)
    real = MockBackend.generate

    async def slow(self, messages, **kw):
        if (kw.get("meta") or {}).get("step") == "draft":
            await asyncio.sleep(0.3)
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "generate", slow)
    s = start(c)
    s, cmd = run(c, s["id"], "next", "n1")   # arrives while unit 1 is being prefetched
    prefetch_idle(c, s["id"])
    log = c.app_ref.state.runner.prefetch_log
    assert cmd["status"] == "completed" and s["reading"]["guides"]["1"].get("prefetched") is True
    assert (s["id"], "cancelled") not in log and (s["id"], "written") in log


def test_a_guide_interrupted_under_older_gate_rules_is_rechecked_not_resumed(tmp_path):
    """Review counterexample: Gate 1 passed under the old rules, the process stopped in draft, and the resumed run
    under new rules continued the subgraph from its checkpoint (old op, old verification) and published it. The
    runner now restarts such a run from the start, under the new rules, keeping what it already spent."""
    proc(tmp_path, START, fault="before:draft")              # unit 0: understood and verified, cut before draft
    proc_env = {"GATE_RULES": "gate1-test-next"}
    env = child_env(HOME=str(tmp_path), **proc_env)
    p = subprocess.run([sys.executable, str(HELPERS / "workflow_proc.py"), str(tmp_path), json.dumps(WAIT)],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["guides"] == ["0"] and out["guide_rules"] == {"0": "gate1-test-next"}
    nodes = [n for n, _ in out["calls"]]
    assert nodes.count("understand") == 2                    # the source was verified again under the new rules
    assert out["guide_calls"]["0"] >= len([n for n in nodes if n not in ("segment",) and not n.startswith("segment")])


# -- 並行數量: sources of coming units are verified while the reader reads ----------------------------------

LONG = "# 光合作用\n\n" + "\n\n".join(
    f"## 第 {i} 節\n\n第 {i} 節說明光合作用的第 {i} 個要點：葉綠體在光照下把二氧化碳和水合成葡萄糖並釋放氧氣，"
    f"這一步需要光、水與二氧化碳同時具備，缺少任何一項都無法進行，因此第 {i} 節特別強調這三個條件。" for i in range(1, 7)) + "\n"


def test_parallel_setting_warms_coming_sources_and_their_guides_skip_source_verification(graph_app, monkeypatch):
    c = graph_app
    monkeypatch.setattr(config, "READING_PREFETCH", True)
    cfg = {"backend": "mock", "model": "", "temperature": 0.2, "max_tokens": 512, "context_tokens": 8192, "parallel": 3}
    assert c.put("/api/settings", json=cfg).status_code == 200
    runner = c.app_ref.state.runner
    assert runner.deps.pool.parallel == 3
    s = start(c, LONG)
    sid = s["id"]
    assert s["reading"]["total"] >= 4
    prefetch_idle(c, sid)
    warmed = [o for x, outs in runner.warm_log if x == sid for o in outs]
    assert warmed and set(warmed) == {"verified"}                     # units 2 and 3 (unit 1 is the prefetch's)
    s, _ = run(c, sid, "next", "n1")
    prefetch_idle(c, sid)
    s, _ = run(c, sid, "next", "n2")                                  # now on unit 2, warmed before the reader got there
    u2 = s["reading"]["quotes"][2]["unit_id"]
    calls = [x for x in c.get(f"/api/sessions/{sid}/trace").json()["llm_calls"]
             if x["task"] == "guide" and u2 in (x["operation_key"] or "") and ":warm:" not in x["operation_key"]]
    assert calls and not any(x["node"] == "understand" for x in calls)  # its source came from the cache
    assert s["reading"]["guides"]["2"]["review_status"] in ("passed", "passed_with_warnings")


def test_parallel_one_never_warms(graph_app, monkeypatch):
    c = graph_app
    monkeypatch.setattr(config, "READING_PREFETCH", True)
    s = start(c, LONG)
    prefetch_idle(c, s["id"])
    assert c.app_ref.state.runner.warm_log == []


# -- deleting a learning record -------------------------------------------------------------------------

def _rows(db, sid):
    with db.conn() as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE session_id=?", (sid,)).fetchone()[0]
                for t in ("turns", "workflow_commands", "llm_calls", "reading_artifacts")}


def _threads(db, sid):
    import sqlite3
    with sqlite3.connect(Path(db.path).with_name("checkpoints.sqlite3")) as c:
        return [r[0] for r in c.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?", (f"%{sid}%",))]


def test_deleting_a_record_removes_everything_it_owns_and_nothing_else(graph_app, monkeypatch):
    monkeypatch.setattr(config, "READING_PREFETCH", True)   # a prefetch thread and artifact must go too
    c, db = graph_app, graph_app.app_ref.state.db
    keep, s = start(c), start(c)
    s, _ = run(c, s["id"], "ask", "q1", payload={"question": "葉綠體是什麼？"})
    c.portal.call(c.app_ref.state.runner.idle)
    sid = s["id"]
    workspace = c.app_ref.state.svc.reading_dir / sid
    assert any(_rows(db, sid).values()) and _threads(db, sid) and workspace.exists()

    assert c.delete(f"/api/sessions/{sid}").status_code == 200
    assert c.get(f"/api/sessions/{sid}").status_code == 404
    assert [x["id"] for x in c.get("/api/sessions").json()] == [keep["id"]]
    assert not any(_rows(db, sid).values()) and not _threads(db, sid) and not workspace.exists()
    # the other record and the shared source snapshot are untouched
    assert any(_rows(db, keep["id"]).values()) and _threads(db, keep["id"])
    assert c.get(f"/api/sessions/{keep['id']}").json()["reading"]["guides"]
    assert c.delete(f"/api/sessions/{sid}").status_code == 404


def test_a_record_with_a_pending_command_is_not_deleted(graph_app):
    c = graph_app
    s = start(c)
    c.app_ref.state.repo.insert_command(s["id"], "x1", "next", {}, "h", None)   # queued, not yet picked up
    r = c.delete(f"/api/sessions/{s['id']}")
    assert r.status_code == 409 and r.json()["code"] == "busy"
    assert c.get(f"/api/sessions/{s['id']}").status_code == 200


def test_an_old_flow_record_can_be_deleted(client):
    from conftest import answer, start as start_legacy
    s = start_legacy(client)
    answer(client, s["id"], "我不知道")
    assert client.delete(f"/api/sessions/{s['id']}").status_code == 200
    assert client.get(f"/api/sessions/{s['id']}").status_code == 404 and client.get("/api/sessions").json() == []


# -- a 主題 written one 區塊 at a time (docs/reading-block-guide-plan.md) -----------------------------------

TOPIC = ("# 光合作用\n\n"
         "光合作用是植物利用光能，把二氧化碳和水轉換成葡萄糖與氧氣的過程，它發生在植物細胞的葉綠體中，是植物累積有機物的主要途徑。\n\n"
         "光反應發生在類囊體膜上：葉綠素吸收光能後把水分解，釋放出氧氣，並產生 ATP 與 NADPH 供後面的反應使用，這一步把光能轉換成化學能儲存起來。\n\n"
         "暗反應發生在葉綠體的基質中：它利用光反應產生的 ATP 與 NADPH，把二氧化碳固定成葡萄糖，這個循環稱為卡爾文循環。\n")


def failing_draft_for(monkeypatch, marker):
    """The simulated drafter writes nothing usable for the 區塊 that contains `marker`."""
    real = MockBackend.generate

    async def gen(self, messages, **kw):
        m = kw.get("meta") or {}
        if m.get("step") in ("draft", "revise_body") and marker in (m.get("quote_text") or ""):
            kw = {**kw, "meta": {**m, "force_invalid_output": True}}
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "generate", gen)


def test_a_topic_is_guided_one_block_at_a_time(graph_app):
    c = graph_app
    s = start(c, TOPIC)
    q = s["reading"]["quotes"][0]
    g = s["reading"]["guides"]["0"]
    assert len(q["chunks"]) == 3 and g["mode"] == "blocks" and not g["partial"]
    assert [b["status"] for b in g["blocks"]] == ["accepted"] * 3 and g["topic_summary"] and g["check"]
    assert [b["source"][:4] for b in g["blocks"]] == ["光合作用", "光反應發", "暗反應發"]
    ids = [c_["id"] for c_ in g["source_claims"]]
    assert len(ids) == len(set(ids)) and {c_["block"] for c_ in g["source_claims"]} == {0, 1, 2}   # one numbering
    assert g["guide"] == "\n\n".join(b["guide"] for b in g["blocks"])
    calls = c.app_ref.state.repo.list_llm_calls(s["id"])   # with the requests (the trace API leaves them out)
    drafts = [x for x in calls if x["node"] == "draft"]
    assert len(drafts) == 3 and len([x for x in calls if x["node"] == "topic"]) == 1
    second = next(x for x in drafts if "-b2:" in x["operation_key"])["request"]["messages"][0]["content"]
    assert "▶ 第 2 塊" in second and "只寫第 2 塊原文裡的內容" in second and "暗反應發生在" not in second.split("## 這一段的原文")[1].split("##")[0]


def test_a_failed_block_leaves_the_topic_readable_and_only_it_is_retried(graph_app, monkeypatch):
    c = graph_app
    failing_draft_for(monkeypatch, "暗反應")
    s = start(c, TOPIC)
    g = s["reading"]["guides"]["0"]
    assert s["workflow"]["waiting"] == "student" and g["partial"]
    assert [b["status"] for b in g["blocks"]] == ["accepted", "accepted", "failed"]
    assert all(cl["block"] != 2 for cl in g["source_claims"])           # no claims from a 區塊 not shown as guided
    n = config.READING_BLOCK_ATTEMPTS                                    # retried automatically before giving up
    assert g["blocks"][2]["attempt"] == n and len(g["blocks"][2]["failed_attempts"]) == n - 1
    monkeypatch.undo()
    before = len(c.get(f"/api/sessions/{s['id']}/trace").json()["llm_calls"])
    s, cmd = run(c, s["id"], "retry_blocks", "rb1")
    g = s["reading"]["guides"]["0"]
    assert cmd["status"] == "completed" and not g["partial"] and [b["status"] for b in g["blocks"]] == ["accepted"] * 3
    calls = c.get(f"/api/sessions/{s['id']}/trace").json()["llm_calls"][before:]
    assert calls and all("-b3:" in x["operation_key"] for x in calls if x["task"] == "guide")   # only the failed 區塊
    assert g["blocks"][2]["attempt"] == config.READING_BLOCK_ATTEMPTS + 1   # after the automatic attempts
    r = c.post(f"/api/sessions/{s['id']}/events", json={"command_id": "rb2", "type": "retry_blocks"})
    assert r.status_code == 409 and r.json()["code"] == "not_ready"                            # nothing left to retry


def test_a_topic_whose_blocks_all_fail_waits_for_retry(graph_app, monkeypatch):
    c = graph_app
    failing_draft_for(monkeypatch, "")                                    # every 區塊
    s = start(c, TOPIC)
    assert s["workflow"]["waiting"] == "retry" and "0" not in s["reading"]["guides"]
    assert "第 1 塊" in s["workflow"]["error"]["detail"] and s["workflow"]["error"]["retryable"]


def test_a_block_that_fails_once_is_written_again_automatically(graph_app, monkeypatch):
    real = MockBackend.generate
    seen = {"draft": 0}

    async def gen(self, messages, **kw):
        m = kw.get("meta") or {}
        if m.get("step") in ("draft", "revise_body") and "暗反應" in (m.get("quote_text") or ""):
            seen["draft"] += 1
            if seen["draft"] <= 2:                                       # the first attempt's draft and its format fix
                kw = {**kw, "meta": {**m, "force_invalid_output": True}}
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "generate", gen)
    s = start(graph_app, TOPIC)
    g = s["reading"]["guides"]["0"]
    assert not g["partial"] and [b["status"] for b in g["blocks"]] == ["accepted"] * 3
    b = g["blocks"][2]
    assert b["attempt"] == 2 and [f["code"] for f in b["failed_attempts"]] == ["invalid_model_output"]


def test_only_failures_a_new_run_can_fix_are_retried():
    from app.reading.topic_guide import worth_retrying
    assert worth_retrying({"code": "quality_check_failed", "detail": "主要正文照抄了 2 句英文原文"})
    assert worth_retrying({"code": "invalid_model_output", "detail": "draft: JSON 解析失敗"})
    assert worth_retrying({"code": "notation_unverified", "detail": "PDF 符號的視覺判讀與字型線索不一致：…"})
    assert not worth_retrying({"code": "notation_unverified", "detail": "目前的模型不支援影像輸入，無法看圖核對"})
    assert not worth_retrying({"code": "context_overflow", "detail": "輸入約 40000 tokens"})
    assert not worth_retrying({"code": "context_missing", "detail": "前後文讀取失敗"})
