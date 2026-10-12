"""The reader's 導讀 mode (app.reading.tour): a window of pages around where the reader is, segmented and guided on
demand in the order of the 主題 on screen; windows never overlap and meet into one run as the reader scrolls on."""
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
from app.reading import tour as tour_mod
from app.reading.tour import GuideTour

MD = (Path(__file__).parent / "helpers" / "flow.md").read_text(encoding="utf-8")


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "READING_PREFETCH", False)
    monkeypatch.setattr(tour_mod, "LINES_PER_PAGE", 1)   # flow.md has 11 lines: one "page" around a line is one line
    app = create_app(Database(tmp_path / "db.sqlite3"), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as client:
        yield client


def book(c):
    return c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()


def settled(c, mid, timeout=30):
    """The tour once every 主題 of the run on screen has its guide (or failed)."""
    end = time.time() + timeout
    while time.time() < end:
        v = c.get(f"/api/materials/{mid}/tour").json()
        ws = v["windows"]
        if ws and all(w["status"] == "ready" for w in ws) and all(t["state"] in ("done", "failed") for w in ws for t in w["topics"]):
            return v
        time.sleep(0.05)
    raise AssertionError(f"tour did not settle: {v}")


def test_open_makes_one_window_around_the_page_and_guides_all_of_it(c):
    m = book(c)
    v = c.post(f"/api/materials/{m['id']}/tour/open", json={"at": 7, "span": 2}).json()
    [w] = v["windows"]
    assert (w["start"], w["end"]) == (5, 9)
    assert v["focus"]["session_id"] == w["session_id"] and v["active"]
    v = settled(c, m["id"])
    topics = v["windows"][0]["topics"]
    assert topics and all(t["state"] == "done" and t["guide"] for t in topics)
    assert all(5 <= t["from"] <= t["to"] <= 9 for t in topics)
    # opened again on a page it covers: the same window, nothing made again
    again = c.post(f"/api/materials/{m['id']}/tour/open", json={"at": 5, "span": 2}).json()
    assert [x["session_id"] for x in again["windows"]] == [w["session_id"]]
    # listed as a 導讀 of the book, its guides written so far as its progress
    listed = {x["id"]: x for x in c.get("/api/sessions").json()}[w["session_id"]]
    assert listed["tour"] is True and listed["progress"]["read"] == listed["progress"]["total"] == len(topics)


def test_windows_never_overlap_and_extend_into_one_run(c):
    m = book(c)
    first = c.post(f"/api/materials/{m['id']}/tour/open", json={"at": 7, "span": 1}).json()["windows"][0]
    assert (first["start"], first["end"]) == (6, 8)
    v = c.post(f"/api/materials/{m['id']}/tour/extend",
               json={"session_id": first["session_id"], "direction": "before", "span": 1}).json()
    assert [(w["start"], w["end"]) for w in v["windows"]] == [(3, 5), (6, 8)]
    assert v["focus"]["session_id"] == v["windows"][0]["session_id"]
    v = c.post(f"/api/materials/{m['id']}/tour/extend",
               json={"session_id": first["session_id"], "direction": "after", "span": 1}).json()
    assert [(w["start"], w["end"]) for w in v["windows"]] == [(3, 5), (6, 8), (9, 11)]
    assert v["last"] and not v["first"]
    settled(c, m["id"])
    # a page next to the run, opened directly: cut short where the run begins; lines 1–2 (the title and a blank line)
    # have nothing to guide, and cannot grow further: blank, and the run's start is the start of the book
    r = c.post(f"/api/materials/{m['id']}/tour/open", json={"at": 2, "span": 2})
    assert r.status_code == 400 and r.json()["code"] == "no_text"
    v = c.post(f"/api/materials/{m['id']}/tour/extend",
               json={"session_id": first["session_id"], "direction": "before", "span": 1}).json()
    assert [(w["start"], w["end"]) for w in v["windows"]] == [(3, 5), (6, 8), (9, 11)] and v["first"]


def test_blank_pages_make_the_window_grow_until_it_has_text(c):
    m = book(c)
    # line 4 is blank, line 5 the heading 光反應: nothing to guide on its own, so the window grows to take line 7
    w = c.post(f"/api/materials/{m['id']}/tour/open", json={"at": 4, "span": 0}).json()["windows"][0]
    assert w["start"] < 4 or w["end"] > 5
    settled(c, m["id"])


def test_pause_stops_the_work_and_focus_takes_it_up_again(c):
    m = book(c)
    w = c.post(f"/api/materials/{m['id']}/tour/open", json={"at": 7, "span": 2}).json()["windows"][0]
    v = c.post(f"/api/materials/{m['id']}/tour/pause").json()
    assert v["active"] is False
    v = c.post(f"/api/materials/{m['id']}/tour/focus", json={"session_id": w["session_id"], "index": 0}).json()
    assert v["active"] is True
    settled(c, m["id"])


def test_a_reader_question_on_a_window_page_uses_its_units(c):
    m = book(c)
    c.post(f"/api/materials/{m['id']}/tour/open", json={"at": 7, "span": 2})
    v = settled(c, m["id"])
    t = v["windows"][0]["topics"][0]
    rec = c.post(f"/api/materials/{m['id']}/ask", json={"at": t["from"], "question": "這段在講什麼？"}).json()
    assert rec["unit_ids"] == [t["unit_id"]] and rec["used_guide"] is True


def test_the_focus_page_comes_first_then_outwards():
    quotes = [{"index": i, "from": p, "to": p} for i, p in enumerate([10, 11, 12, 12, 13, 14])]
    key = GuideTour._priority(quotes, {"index": None, "at": 12})
    assert sorted(range(6), key=key) == [2, 3, 4, 1, 5, 0]
    key = GuideTour._priority(quotes, {"index": 4, "at": 12})
    assert sorted(range(6), key=key) == [4, 5, 3, 2, 1, 0]
