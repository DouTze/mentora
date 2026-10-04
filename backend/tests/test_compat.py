"""R7: data written by the pre-change code (commit 38e3c11, reading-graph-v1, schema v4) keeps working: the database
migrates (v5), the outer checkpoints load, a session waiting for the reader is upgraded once (recorded) and
continues under the v2 reading subgraph; its published v1 guide is kept as it was; a session already in the quiz
answers as before; exports say which check produced each guide."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import Database
from app.llm.registry import Registry
from app.main import create_app
from app.models import ModelConfig
from app.reading.agent import READING_WORKFLOW_VERSION
from app.workflows.migrations import upgrade_session
from test_workflow import idle, run

FIX = Path(__file__).parent / "fixtures" / "v1_session"


@pytest.fixture
def old_app(tmp_path, monkeypatch):
    for f in ("db.sqlite3", "checkpoints.sqlite3"):
        shutil.copy(FIX / f, tmp_path / f)
    monkeypatch.setattr(config, "WORKFLOW", "langgraph")
    monkeypatch.setattr(config, "READING_PREFETCH", False)
    app = create_app(Database(tmp_path / "db.sqlite3"), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def ids():
    waiting, quiz = (FIX / "ids.txt").read_text().split()
    return waiting, quiz


def test_old_database_migrates_and_a_waiting_session_continues_under_v2(old_app):
    c = old_app
    sid, _ = ids()
    db = c.app_ref.state.db
    with db.conn() as conn:
        assert {r[0] for r in conn.execute("SELECT version FROM schema_migrations")} >= {1, 2, 3, 4, 5}
    before = db.get_session(sid)
    assert before["workflow_meta"]["reading_workflow_version"] == "reading-graph-v1"
    old_guide = before["state"]["reading"]["guides"]["0"]
    s, cmd = run(c, sid, "next", "n-after-upgrade")
    assert cmd["status"] == "completed", cmd
    after = db.get_session(sid)
    m = after["workflow_meta"]
    assert m["reading_workflow_version"] == READING_WORKFLOW_VERSION and m["task_contract"]["defaults"]
    assert len(m["migrations"]) == 1 and m["migrations"][0]["from"] == "reading-graph-v1"
    assert m["prompt_bundle"]["upgraded_from"] == before["workflow_meta"]["prompt_bundle"]["hash"]
    assert m["prompt_bundle"]["prompts"]["ask"] == before["workflow_meta"]["prompt_bundle"]["prompts"]["ask"]   # kept
    rd = after["state"]["reading"]
    assert rd["guides"]["0"] == old_guide                                  # the published v1 guide is untouched
    g1 = rd["guides"]["1"]
    assert g1["workflow_version"] == READING_WORKFLOW_VERSION and g1["gate1"]["status"] == "pass"
    assert len(rd["qa"]["0"]) == 1                                        # the question asked under v1 is kept
    assert upgrade_session(db, sid) is None                               # idempotent
    md = c.get(f"/api/sessions/{sid}/export?format=md").text
    assert "reading-graph-v1" in md and READING_WORKFLOW_VERSION in md


def test_an_upgraded_session_quizzes_v1_units_from_their_source_only(old_app):
    c = old_app
    sid, _ = ids()
    s = idle(c, sid)
    k = 0
    while s["workflow"]["waiting"] == "student":
        s, cmd = run(c, sid, "next", f"n{k}")
        assert cmd["status"] == "completed", cmd
        k += 1
    content = c.app_ref.state.db.get_session(sid)["content_snapshot"]
    bases = {b for cc in content["concepts"] for b in cc["basis"]}
    assert content["concepts"] and "Q1.SRC" in bases    # unit 0 was read under v1: only its source text is a basis
    md = c.get(f"/api/sessions/{sid}/export?format=md").text
    assert "## 題目依據" in md


def test_a_session_already_in_the_quiz_answers_as_before(old_app):
    c = old_app
    _, sid = ids()
    s = idle(c, sid)
    assert s["workflow"]["waiting"] == "answer"
    s, cmd = run(c, sid, "answer", "a1", payload={"answer": "因為需要光能"})
    assert cmd["status"] == "completed" and cmd["result"]["decision"]


def test_legacy_sessions_are_never_upgraded(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    row = db.create_session({"source": {}}, {}, {}, {"phase": "reading"})
    assert upgrade_session(db, row["id"]) is None and db.get_session(row["id"])["workflow_meta"] is None


def test_upgrading_a_pdf_session_backfills_layout_evidence_from_the_stored_file(tmp_path, monkeypatch):
    """A v1 PDF session's blocks carry no evidence; the upgrade fills it from the original file (text unchanged)."""
    import io
    import json as _json
    from app.workflows.migrations import upgrade_session
    monkeypatch.setattr(config, "WORKFLOW", "langgraph")
    monkeypatch.setattr(config, "READING_PREFETCH", False)
    app = create_app(Database(tmp_path / "db.sqlite3"), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as c:
        data = (Path(__file__).parent.parent / "evals/reading/pdf/symbols.pdf").read_bytes()
        m = c.post("/api/materials", files={"file": ("symbols.pdf", io.BytesIO(data))}).json()
        s = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 1}).json()
        sid = s["id"]
        idle(c, sid)
        db = app.state.db
        snap = db.get_session(sid)["workflow_meta"]["snapshot_id"]
        texts = [b.text for b in app.state.repo.load_blocks(snap)]
        with db.conn() as conn:   # make it look like a pre-R5, reading-graph-v1 session
            conn.execute("UPDATE source_blocks SET meta=NULL WHERE snapshot_id=?", (snap,))
            meta = db.get_session(sid)["workflow_meta"]
            meta["reading_workflow_version"] = "reading-graph-v1"
            conn.execute("UPDATE sessions SET workflow_meta=? WHERE id=?", (_json.dumps(meta, ensure_ascii=False), sid))
        rec = upgrade_session(db, sid)
        assert rec["pdf_evidence"] == "backfilled"
        blocks = app.state.repo.load_blocks(snap)
        assert [b.text for b in blocks] == texts and any((b.meta.get("pdf") or {}).get("bold_tokens") == ["w"] for b in blocks)
