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


def guided(c, sid, timeout=20):
    """The session once its first guide is in."""
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/sessions/{sid}").json()
        if "0" in s["reading"]["guides"]:
            return s
        time.sleep(0.02)
    raise AssertionError("no guide")


def test_learn_from_the_reader_reuses_the_study_units_and_guides(c):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    v = ready(c, m["id"])
    db = c.app.state.db
    study = db.get_session(v["session_id"])
    guided(c, study["id"])                                   # the study wrote the guide of its first unit
    part = v["parts"][0]
    s = c.post(f"/api/materials/{m['id']}/learn", json={"at": part["from"]}).json()
    row = db.get_session(s["id"])
    # the part's units as the study cut them: same snapshot and unit ids, nothing segmented again
    sp = study["content_snapshot"]["parts"][0]
    want = [q["unit_id"] for q in study["content_snapshot"]["quotes"][sp["quote_from"]:sp["quote_to"] + 1]]
    assert [q["unit_id"] for q in row["content_snapshot"]["quotes"]] == want
    assert [q["index"] for q in row["content_snapshot"]["quotes"]] == list(range(len(want)))
    assert row["workflow_meta"]["snapshot_id"] == study["workflow_meta"]["snapshot_id"]
    seg = row["content_snapshot"]["segmentation"]
    assert seg["status"] == "done" and seg["reused_from"] == study["id"]
    # the study's guide is shown as it is, not written again
    g = guided(c, s["id"])["reading"]["guides"]["0"]
    assert g["reused_from"] == study["id"]
    assert {k: x for k, x in g.items() if k != "reused_from"} == study_guide(db, study["id"], want[0])


def study_guide(db, sid, unit_id):
    import json
    with db.conn() as conn:
        r = conn.execute("SELECT payload FROM reading_artifacts WHERE session_id=? AND unit_id=? AND kind='guide' "
                         "AND status='accepted' ORDER BY created_at DESC", (sid, unit_id)).fetchone()
    return json.loads(r["payload"])


def test_an_older_reading_cut_on_its_own_is_not_taken_up(c):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    v = ready(c, m["id"])
    part = v["parts"][0]
    old = c.app.state.workflow.start_from_material(m["id"], part["from"], part["to"], "rules-v1", notify=False)
    s = c.post(f"/api/materials/{m['id']}/learn", json={"at": part["from"]}).json()
    assert s["id"] != old["id"]
    assert c.app.state.db.get_session(s["id"])["content_snapshot"]["segmentation"]["reused_from"] == v["session_id"]


def test_deleting_a_material_closes_its_unfinished_readings(c):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    v = ready(c, m["id"])
    s = c.post(f"/api/materials/{m['id']}/learn", json={"at": v["parts"][0]["from"]}).json()
    guided(c, s["id"])
    other = c.post("/api/materials", files={"file": ("other.md", io.BytesIO((MD + "\n另一本。\n").encode()))}).json()
    keep = c.app.state.workflow.start_from_material(other["id"], 1, 3, "rules-v1", notify=False)
    r = c.delete(f"/api/materials/{m['id']}").json()
    assert r["closed"] == [s["id"]]
    listed = {x["id"]: x for x in c.get("/api/sessions").json()}
    assert listed[s["id"]]["status"] == "closed"                  # kept as a record, no longer in progress
    assert v["session_id"] not in listed                          # the material's study went with it
    assert listed[keep["id"]]["status"] == "active"               # other books' readings are untouched
    assert c.get(f"/api/sessions/{s['id']}").status_code == 200
    refused = c.post(f"/api/sessions/{s['id']}/events", json={"type": "next"})
    assert refused.status_code == 409 and refused.json()["code"] == "closed"


def test_readings_of_materials_deleted_before_are_closed_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "READING_PREFETCH", False)
    db = Database(tmp_path / "db.sqlite3")
    with TestClient(create_app(db, Registry(ModelConfig(backend="mock")))) as c:
        m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
        s = c.app.state.workflow.start_from_material(m["id"], 1, 3, "rules-v1", notify=False)
        with db.conn() as conn:   # as an older version deleted it: the row gone, the reading left active
            conn.execute("DELETE FROM materials WHERE id=?", (m["id"],))
    with TestClient(create_app(db, Registry(ModelConfig(backend="mock")))) as c:
        assert c.get(f"/api/sessions/{s['id']}").json()["status"] == "closed"


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


def test_pdf_info_layer_image_and_attachment(c):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((40, 60), "Read the documentation online.")
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(40, 48, 220, 64), "uri": "https://example.org"})
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4), False)
    pix.clear_with(90)
    page.insert_image(pymupdf.Rect(40, 100, 80, 140), pixmap=pix)
    doc.embfile_add("data.csv", b"a,b\n1,2\n", filename="資料.csv")
    doc.set_toc([[1, "Only chapter", 1]])
    m = c.post("/api/materials", files={"file": ("info.pdf", io.BytesIO(doc.tobytes()))}).json()
    info = c.get(f"/api/materials/{m['id']}/info").json()
    assert info["page_count"] == 1 and info["outline"][0]["title"] == "Only chapter"
    assert info["counts"]["links"] == 1 and info["attachments"][0]["name"] == "資料.csv"
    layer = c.get(f"/api/materials/{m['id']}/pages/1/layer").json()
    assert " ".join(r["t"] for r in layer["lines"][0]["runs"]) == "Read the documentation online."
    assert layer["links"] == [{"rect": [40, 48, 220, 64], "kind": "uri", "uri": "https://example.org"}]
    assert c.get(f"/api/materials/{m['id']}/pages/1/layer").json() == layer       # served from the cache
    img = c.get(f"/api/materials/{m['id']}/images/{layer['images'][0]['xref']}")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/")
    att = c.get(f"/api/materials/{m['id']}/attachments/0")
    assert att.content == b"a,b\n1,2\n" and "%E8%B3%87%E6%96%99.csv" in att.headers["content-disposition"]
    assert c.get(f"/api/materials/{m['id']}/pages/2/layer").status_code == 404
    assert c.get(f"/api/materials/{m['id']}/attachments/1").status_code == 404
    md = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    assert c.get(f"/api/materials/{md['id']}/info").status_code == 404


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
    b = c.post("/api/materials", files={"file": ("b.md", io.BytesIO((MD + "\n另一本。\n").encode()))}).json()   # not the same file
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
