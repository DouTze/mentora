"""A PDF's text is read after its import, in the background: the import answers at once with the book recorded (its
pages counted), what needs the text waits (409 `preparing`), and a book left half read is read at the next start."""
import io
import time

import pymupdf
from fastapi.testclient import TestClient

from app import config
from app.db import Database
from app.llm.registry import Registry
from app.main import create_app
from app.models import ModelConfig


def two_pages() -> bytes:
    doc = pymupdf.open()
    for i in range(2):
        doc.new_page(width=300, height=400).insert_text((40, 60), f"Page {i + 1} explains correlation and causation.")
    return doc.tobytes()


def wait_ready(c, mid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        m = c.get(f"/api/materials/{mid}").json()
        if m["status"] != "extracting":
            return m
        time.sleep(.2)
    raise AssertionError("the text was not read")


def test_a_pdf_is_recorded_at_once_and_its_text_read_in_the_background(db_path, monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_IN_BACKGROUND", True)
    app = create_app(Database(db_path), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as c:
        m = c.post("/api/materials", files={"file": ("bg.pdf", io.BytesIO(two_pages()))}).json()
        assert m["status"] == "extracting" and m["unit_count"] == 2      # recorded, pages counted, text not read yet
        assert c.get(f"/api/materials/{m['id']}/pages").json()["sizes"] == [[300.0, 400.0]] * 2   # it opens already
        ready = wait_ready(c, m["id"])
        assert ready["status"] == "ready" and "Page 2 explains" in ready["units"][1]
        # what needs the text waits while it is being read
        with app.state.db.conn() as conn:
            conn.execute("UPDATE materials SET status='extracting' WHERE id=?", (m["id"],))
        r = c.post(f"/api/materials/{m['id']}/ask", json={"at": 1, "question": "這頁在說什麼？"})
        assert r.status_code == 409 and r.json()["code"] == "preparing"
        assert c.post(f"/api/materials/{m['id']}/learn", json={"at": 1}).status_code == 409
        view = c.get(f"/api/materials/{m['id']}/study").json()
        assert view["status"] == "reading" and view["step"] == {"phase": "extract"}
    # left half read when the app stopped: read at the next start
    app = create_app(Database(db_path), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as c:
        assert wait_ready(c, m["id"])["status"] == "ready"


def test_a_pdf_without_text_says_so_after_the_import(db_path, monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_IN_BACKGROUND", True)
    doc = pymupdf.open()
    doc.new_page(width=300, height=400)   # a page with nothing to read (a scan would be the same)
    app = create_app(Database(db_path), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as c:
        m = c.post("/api/materials", files={"file": ("blank.pdf", io.BytesIO(doc.tobytes()))}).json()
        assert m["status"] == "extracting"
        failed = wait_ready(c, m["id"])
        assert failed["status"] == "failed" and "文字" in failed["status_detail"]
        r = c.post(f"/api/materials/{m['id']}/ask", json={"at": 1, "question": "?"})
        assert r.status_code == 400 and r.json()["code"] == "unreadable"
