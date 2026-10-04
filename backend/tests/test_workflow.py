"""Durable learning flow (P4): events API, idempotent commands, serial sessions, retries, and restarts at every
fault window — business commit before checkpoint, resume before command completion, inside the reading subgraph,
between planning windows."""
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
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
    monkeypatch.setattr(config, "WORKFLOW", "langgraph")
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


def test_legacy_endpoints_refuse_graph_sessions_and_legacy_sessions_stay_legacy(graph_app, monkeypatch):
    c = graph_app
    s = start(c)
    for path in ("reading/guide", "reading/next", "reading/finish"):
        r = c.post(f"/api/sessions/{s['id']}/{path}")
        assert r.status_code == 409 and r.json()["code"] == "use_events"
    assert c.post(f"/api/sessions/{s['id']}/answer", json={"answer": "x"}).json()["code"] == "use_events"
    monkeypatch.setattr(config, "WORKFLOW", "legacy")
    m = c.post("/api/materials", files={"file": ("x.md", io.BytesIO(MD.encode()))}).json()
    legacy = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 5}).json()
    assert legacy["workflow_runner"] == "legacy" and legacy["workflow"] is None
    r = c.post(f"/api/sessions/{legacy['id']}/events", json={"command_id": "x", "type": "next"})
    assert r.status_code == 409 and r.json()["code"] == "legacy_session"
    assert c.get(f"/api/sessions/{s['id']}").json()["workflow_runner"] == "langgraph"   # pinned at creation


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
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)} | ({"MENTOR_FAULT": fault} if fault else {})
    p = subprocess.run([sys.executable, str(HELPERS / "workflow_proc.py"), str(tmp_path), json.dumps(steps)],
                       capture_output=True, text=True, env=env, timeout=120)
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
    assert [n for n, _ in out["calls"]].count("segment.w01") == 1


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
    monkeypatch.setattr(config, "WORKFLOW", "langgraph")
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
    prefetch_calls = [x for x in calls if x["command_id"] == "prefetch" and f":prefetch:{unit1}:" in x["operation_key"]]
    assert len({x["operation_key"].split(":run")[0] for x in prefetch_calls}) == 2   # before / after the question
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
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)} | proc_env
    p = subprocess.run([sys.executable, str(HELPERS / "workflow_proc.py"), str(tmp_path), json.dumps(WAIT)],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["guides"] == ["0"] and out["guide_rules"] == {"0": "gate1-test-next"}
    nodes = [n for n, _ in out["calls"]]
    assert nodes.count("understand") == 2                    # the source was verified again under the new rules
    assert out["guide_calls"]["0"] >= len([n for n in nodes if n not in ("segment",) and not n.startswith("segment")])
