"""The reader page: the companion's whole-material study (a normal learning session over every unit), questions asked
from the page being read, 導讀 started from where the reader is, and PDF pages drawn as images."""
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import Database
from app.llm.registry import Registry
from app.main import create_app
from app.models import ModelConfig

MD = (Path(__file__).parent / "helpers" / "flow.md").read_text(encoding="utf-8")


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "READING_PREFETCH", False)
    app = create_app(Database(tmp_path / "db.sqlite3"), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as client:
        yield client


def created(c, mid, timeout=20):
    """The study session once its set-up (in a child process) has finished."""
    end = time.time() + timeout
    while time.time() < end:
        row = c.app.state.study.session(mid)
        if row:
            return row
        time.sleep(0.01)
    raise AssertionError("study was not created")


def ready(c, mid, timeout=20):
    c.post(f"/api/materials/{mid}/study/focus")
    end = time.time() + timeout
    while time.time() < end:
        v = c.post(f"/api/materials/{mid}/study/focus").json()
        if v["status"] != "reading":
            return v
        time.sleep(0.02)
    raise AssertionError("study did not finish")


def test_study_is_one_whole_material_session_reused(c):
    lines = len(MD.splitlines())
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    v = ready(c, m["id"])
    assert v["status"] == "ready" and v["parts"]
    assert v["parts"][0]["from"] >= 1 and v["parts"][-1]["to"] <= lines
    s = c.get(f"/api/sessions/{v['session_id']}").json()
    assert (s["source"]["start"], s["source"]["end"]) == (1, lines)
    # opened again: the same study, not another one
    assert c.post(f"/api/materials/{m['id']}/study/focus").json()["session_id"] == v["session_id"]
    listed = {x["id"]: x for x in c.get("/api/sessions").json()}
    assert listed[v["session_id"]]["study"] is True


def test_learn_from_the_reader_starts_the_part_range_once(c):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    v = ready(c, m["id"])
    part = v["parts"][-1]
    s = c.post(f"/api/materials/{m['id']}/learn", json={"at": part["to"]}).json()
    src = s["source"]
    assert (src["start"], src["end"]) == (part["from"], part["to"])
    assert s["id"] != v["session_id"]
    again = c.post(f"/api/materials/{m['id']}/learn", json={"at": part["from"]}).json()
    assert again["id"] == s["id"]


def test_reader_question_is_kept(c):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    ready(c, m["id"])
    r = c.post(f"/api/materials/{m['id']}/ask", json={"at": 3, "question": "這段在說什麼？"})
    assert r.status_code == 200, r.text
    qa = r.json()
    assert qa["answer"] and qa["unit_ids"] and qa["unit_no"] == 3
    assert [x["id"] for x in c.get(f"/api/materials/{m['id']}/questions").json()] == [qa["id"]]


def test_pdf_pages_render(c, tmp_path):
    import pymupdf
    doc = pymupdf.open()
    for i in range(2):
        page = doc.new_page(width=300, height=400)
        page.insert_text((40, 60), f"Page {i + 1} talks about correlation and causation in some detail.")
    data = doc.tobytes()
    m = c.post("/api/materials", files={"file": ("two.pdf", io.BytesIO(data))}).json()
    assert c.get(f"/api/materials/{m['id']}/pages").json()["sizes"] == [[300.0, 400.0], [300.0, 400.0]]
    r = c.get(f"/api/materials/{m['id']}/pages/2", params={"w": 700})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert c.get(f"/api/materials/{m['id']}/pages/3").status_code == 404


def test_progress_counts_the_saved_segmentation_batches(c):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    v = ready(c, m["id"])
    assert v["step"] is None
    study = c.app.state.study
    share, step = study._progress(c.app.state.db.get_session(v["session_id"]))
    # every batch is saved once the study is done: the last pass, at its last batch
    assert step["phase"] in ("topic", "window") and 1 <= step["current"] <= step["total"]
    assert 0.5 <= share <= 1


def test_a_study_runs_only_while_its_book_is_read(c):
    a = c.post("/api/materials", files={"file": ("a.md", io.BytesIO(MD.encode()))}).json()
    b = c.post("/api/materials", files={"file": ("b.md", io.BytesIO(MD.encode()))}).json()
    # never opened: nothing runs
    assert c.get(f"/api/materials/{a['id']}/study").json()["status"] == "off"
    runner = c.app.state.runner
    c.post(f"/api/materials/{a['id']}/study/focus")
    sa = created(c, a["id"])
    # opening b holds a's study
    c.post(f"/api/materials/{b['id']}/study/focus")
    assert runner.held(sa["id"])
    vb = ready(c, b["id"])
    assert vb["status"] == "ready"
    # back to a: it runs again and finishes
    assert ready(c, a["id"])["status"] == "ready"
    assert not runner.held(sa["id"])
    c.post(f"/api/materials/{a['id']}/study/leave")
    assert runner.held(sa["id"])


def test_pause_resume_and_cancel_by_the_person(c):
    m = c.post("/api/materials", files={"file": ("a.md", io.BytesIO(MD.encode()))}).json()
    c.post(f"/api/materials/{m['id']}/study/focus")
    created(c, m["id"])
    v = c.post(f"/api/materials/{m['id']}/study/pause").json()
    assert v["by_user"] is True or v["session_id"] is None
    sid = c.app.state.study.session(m["id"])["id"]
    # paused by the person: opening the book again does not resume it
    c.post(f"/api/materials/{m['id']}/study/focus")
    assert c.app.state.runner.held(sid)
    c.post(f"/api/materials/{m['id']}/study/resume")
    assert ready(c, m["id"])["status"] == "ready"
    v = c.post(f"/api/materials/{m['id']}/study/cancel").json()
    assert v["status"] == "off" and v["session_id"] is None
    assert c.get(f"/api/sessions/{sid}").status_code == 404
    # cancelled: opening the book does not start it again; resume does
    assert c.post(f"/api/materials/{m['id']}/study/focus").json()["status"] == "off"
    assert c.post(f"/api/materials/{m['id']}/study/resume").json()["status"] in ("reading", "ready")
