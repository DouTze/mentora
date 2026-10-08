"""Home-page bookshelf: the bounded file walk, keyword ranking, and the scout → shelf → import flow (mock model)."""
import time

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.llm.registry import Registry
from app.main import create_app
from app.materials import MaterialStore
from app.llm.runtime import BackendPool
from app.models import ModelConfig
from app.shelf import index as shelf_index
from app.shelf.books import keywords_of
from app.shelf.service import ShelfService


@pytest.fixture(autouse=True)
def small_files(monkeypatch):
    monkeypatch.setattr(shelf_index, "MIN_BYTES", {"pdf": 100, "md": 10, "txt": 10})


def pdf(path, title, pages=3, body="Sample reading text"):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    words = ["agents", "rewards", "policies", "values", "models", "planning", "exploration", "returns"]
    for i in range(pages):   # different text on every page: a line repeated on all pages is a running header
        doc.new_page().insert_text((72, 72), f"{body}: on {words[i % len(words)]} and {words[(i + 3) % len(words)]}.")
    doc.set_metadata({"title": title, "author": "Tester"})
    doc.save(path)


@pytest.fixture
def books(tmp_path):
    root = tmp_path / "home"
    pdf(root / "Books" / "rl_intro.pdf", "Reinforcement Learning: An Introduction", 5)
    pdf(root / "Books" / "強化學習筆記.pdf", "Untitled")
    pdf(root / "Downloads" / "invoice-2026.pdf", "Invoice")
    (root / "Notes").mkdir()
    (root / "Notes" / "機器學習.md").write_text("# 機器學習導論\n\n" + "內容 " * 50, encoding="utf-8")
    (root / "Notes" / "README.md").write_text("readme " * 50)
    (root / ".hidden").mkdir()
    pdf(root / ".hidden" / "secret.pdf", "Secret")
    (root / "code" / "node_modules").mkdir(parents=True)
    pdf(root / "code" / "node_modules" / "dep.pdf", "Dep")
    (root / "code" / "package.json").write_text("{}")
    (root / "code" / "guide.md").write_text("# Guide\n" + "x " * 100)
    pdf(root / "code" / "paper.pdf", "A Paper")
    return root


def test_scan_skips_hidden_build_and_project_notes(books):
    names = {e.name for e in shelf_index.scan([books])}
    assert {"rl intro", "強化學習筆記", "invoice-2026", "機器學習", "paper"} <= names
    assert not names & {"secret", "dep", "README", "guide"}


def test_chapter_folders_become_one_series_book(tmp_path):
    novel = tmp_path / "Novels" / "某部小說"
    novel.mkdir(parents=True)
    for n in (3, 1, 2, 10, 4):
        (novel / f"某部小說 {n:04d}.txt").write_text(f"第 {n} 章內容 " * 20, encoding="utf-8")
    other = tmp_path / "Novels" / "另一部"
    other.mkdir()
    for n in range(1, 6):
        (other / f"{n:03d} 章名{n}.txt").write_text("內容 " * 20, encoding="utf-8")
    es = {e.name: e for e in shelf_index.scan([tmp_path])}
    assert es["某部小說"].parts == 5 and es["另一部"].parts == 5
    order = [p.rsplit(" ", 1)[1] for p in shelf_index.series_files(es["某部小說"].path)]
    assert order == ["0001.txt", "0002.txt", "0003.txt", "0004.txt", "0010.txt"]


def test_generated_folders_are_skipped(tmp_path):
    (tmp_path / "batch-a8ba948d-bb00-4b3c-a107-030854bc7b3b").mkdir()
    (tmp_path / "batch-a8ba948d-bb00-4b3c-a107-030854bc7b3b" / "draft.md").write_text("x " * 100)
    assert shelf_index.scan([tmp_path]) == []


def test_clean_title_drops_download_site_tags():
    assert shelf_index.clean_title("Deep_Learning (z-lib.org)") == "Deep Learning"
    assert shelf_index.clean_title("sutton-barto.2nd.ed") == "sutton-barto 2nd ed"
    assert shelf_index.clean_title("學習指引-科目3 20251222101907") == "學習指引-科目3"


def test_keywords_split_chinese_runs_and_keep_latin():
    kws = keywords_of("強化學習 RL 入門")
    assert "強化學習" in kws and "強化" in kws and "rl" in kws and "入門" in kws


@pytest.fixture
def client(tmp_path, books):
    db = Database(tmp_path / "t.sqlite3")
    registry = Registry(ModelConfig(backend="mock"))
    shelf = ShelfService(db, registry, BackendPool(), MaterialStore(db), tmp_path / "shelf", roots=[books])
    with TestClient(create_app(db, registry, shelf)) as c:
        yield c


def wait_ready(client):
    for _ in range(100):
        v = client.get("/api/shelf").json()
        if v["status"] == "ready":
            return v
        time.sleep(.05)
    raise AssertionError("scout did not finish")


def test_quick_search_needs_no_model(client):
    client.post("/api/shelf/query", json={"text": ""})   # first run builds the index
    wait_ready(client)
    titles = [b["title"] for b in client.get("/api/shelf/search", params={"q": "reinforcement"}).json()["books"]]
    # the PDF's own title matches first; the lexicon carries the English wish over to the Chinese notes
    assert titles == ["Reinforcement Learning: An Introduction", "強化學習筆記"]


def test_query_runs_scout_saves_interest_and_imports_into_a_reading_area(client):
    r = client.post("/api/shelf/query", json={"text": "強化學習"})
    assert r.status_code == 200
    v = wait_ready(client)
    assert v["interests"] == ["強化學習"]
    assert v["tendencies"][0] == "強化學習"
    titles = [b["title"] for b in v["books"]]
    assert "強化學習筆記" in titles          # an unusable PDF title falls back to the cleaned file name
    assert all(b["id"] and b["kind"] for b in v["books"])

    book = next(b for b in v["books"] if b["title"] == "強化學習筆記")
    cover = client.get(f"/api/shelf/{book['id']}/cover")
    assert cover.status_code == 200 and cover.headers["content-type"] == "image/png"

    r = client.post("/api/shelf/import", json={"ids": [book["id"]], "category_name": "強化學習"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["category"]["name"] == "強化學習"
    assert out["materials"][0]["title"] == "強化學習筆記" and out["materials"][0]["category_id"] == out["category"]["id"]
    assert book["id"] not in [b["id"] for b in client.get("/api/shelf").json()["books"]]


def test_books_join_an_existing_reading_area(client):
    client.post("/api/shelf/query", json={"text": ""})
    v = wait_ready(client)
    area = client.post("/api/categories", json={"name": "已有的閱讀區"}).json()
    book = v["books"][0]
    out = client.post("/api/shelf/import", json={"ids": [book["id"]], "category_id": area["id"], "category_name": "不會用到"}).json()
    assert out["category"]["id"] == area["id"] and out["materials"][0]["category_id"] == area["id"]
    assert [c["name"] for c in client.get("/api/library").json()["categories"]] == ["已有的閱讀區"]   # none made
    other = client.get("/api/shelf").json()["books"][0]
    assert client.post("/api/shelf/import", json={"ids": [other["id"]], "category_id": "gone"}).status_code == 404


def test_import_refuses_ids_not_in_the_index(client):
    r = client.post("/api/shelf/import", json={"ids": ["deadbeef0000"]})
    assert r.status_code == 404


def test_importing_a_series_joins_its_chapters_in_order(client, books):
    novel = books / "Books" / "長篇"
    novel.mkdir()
    for n in (2, 1, 3, 4, 5):
        (novel / f"長篇 {n:03d}.txt").write_text(f"chapter {n} of the long story", encoding="utf-8")
    client.post("/api/shelf/query", json={"text": "長篇"})
    v = wait_ready(client)
    book = next(b for b in v["books"] if b["parts"] == 5)
    m = client.post("/api/shelf/import", json={"ids": [book["id"]]}).json()["materials"][0]
    units = client.get(f"/api/materials/{m['id']}").json()["units"]
    assert [u for u in units if u.strip()] == [f"chapter {n} of the long story" for n in range(1, 6)]


def test_text_files_have_no_rendered_cover(client):
    client.post("/api/shelf/query", json={"text": "機器學習"})
    v = wait_ready(client)
    md = next(b for b in v["books"] if b["kind"] == "md")
    assert md["title"] == "機器學習導論"     # the first # heading
    assert client.get(f"/api/shelf/{md['id']}/cover").status_code == 404


def test_wishes_are_remembered_newest_first_and_can_be_forgotten(client):
    for w in ("小說", "強化學習", "小說"):
        client.post("/api/shelf/query", json={"text": w})
    assert wait_ready(client)["interests"] == ["小說", "強化學習"]
    r = client.request("DELETE", "/api/shelf/interests", json={"text": "小說"})
    assert r.json()["interests"] == ["強化學習"]


def test_without_a_model_wishes_are_widened_by_the_topic_lexicon(client):
    client.post("/api/shelf/query", json={"text": ""})
    v = wait_ready(client)
    assert v["mode"] == "keywords"            # the simulated backend never pretends to have judged the books
    assert all(b["reason"] == "" for b in v["books"])
    titles = [b["title"] for b in client.get("/api/shelf/search", params={"q": "RL"}).json()["books"]]
    assert "Reinforcement Learning: An Introduction" in titles    # RL → reinforcement, through the lexicon


def test_opening_text_is_searched_and_short_terms_match_whole_words(client, books):
    pdf(books / "Books" / "scan_0042.pdf", "Untitled", body="Markov decision processes and the Bellman equation")
    pdf(books / "Books" / "world_atlas.pdf", "Atlas", body="Maps of the world")
    client.post("/api/shelf/query", json={"text": ""})
    wait_ready(client)
    titles = [b["title"] for b in client.get("/api/shelf/search", params={"q": "bellman"}).json()["books"]]
    assert titles == ["scan 0042"]
    titles = [b["title"] for b in client.get("/api/shelf/search", params={"q": "rl"}).json()["books"]]
    assert "Atlas" not in titles and "world atlas" not in titles     # "rl" is not inside "world"


def test_scope_starts_with_the_offered_folders_and_rejects_unknown_ones(client, books):
    sc = client.get("/api/shelf/scope").json()
    assert [(r["path"], r["enabled"]) for r in sc["roots"]] == [(str(books), True)] and sc["exclude"] == []
    r = client.put("/api/shelf/scope", json={"roots": [{"path": str(books / "nope"), "enabled": True}]})
    assert r.status_code == 400


def test_excluding_a_folder_removes_its_books_covers_and_text(client, books, tmp_path):
    client.post("/api/shelf/query", json={"text": "reinforcement"})
    v = wait_ready(client)
    rl = next(b for b in v["books"] if b["title"].startswith("Reinforcement"))
    assert client.get(f"/api/shelf/{rl['id']}/cover").status_code == 200
    r = client.put("/api/shelf/scope", json={"roots": [{"path": str(books), "enabled": True}], "exclude": [str(books / "Books")]})
    assert r.status_code == 200, r.text
    assert [x["label"] for x in r.json()["exclude"]] == [str((books / "Books").resolve())]
    v = wait_ready(client)
    assert all(b["folder"] != "Books" for b in v["books"])
    assert client.get(f"/api/shelf/{rl['id']}/cover").status_code == 404
    shelf_dir = tmp_path / "shelf"
    assert not any(f.name.startswith(rl["id"]) for f in (shelf_dir / "covers").iterdir())
    assert rl["id"] not in (shelf_dir / "meta.json").read_text(encoding="utf-8")
    assert client.get("/api/shelf/search", params={"q": "reinforcement"}).json()["books"] == []


def test_switching_every_folder_off_empties_the_shelf(client, books):
    client.put("/api/shelf/scope", json={"roots": [{"path": str(books), "enabled": False}]})
    v = wait_ready(client)
    assert v["books"] == [] and v["indexed"] == 0 and v["scoped"] is False


def test_imported_pdf_has_a_cover_and_sessions_report_progress(client):
    client.post("/api/shelf/query", json={"text": "reinforcement"})
    book = next(b for b in wait_ready(client)["books"] if b["kind"] == "pdf")
    r = client.post("/api/shelf/import", json={"ids": [book["id"]]})
    assert r.status_code == 200, r.text
    m = r.json()["materials"][0]
    cover = client.get(f"/api/materials/{m['id']}/cover")
    assert cover.status_code == 200 and cover.headers["content-type"] == "image/png"
    r = client.post("/api/sessions", json={"content_id": "correlation-causation", "strategy_id": "rules-v1"})
    assert r.status_code == 200
    assert client.get("/api/sessions").json()[0]["progress"]["stage"] in ("reading", "quiz")


def test_every_import_leaves_a_copy_in_the_library_folder(client, tmp_path):
    from app.library import sha256, stored_name
    lib = tmp_path / "library"
    client.app.state.svc.materials.library_dir = lib    # the store behind /api/materials
    data = "第一行\n第二行\n".encode()
    r = client.post("/api/materials", files={"file": ("notes.txt", data, "text/plain")})
    assert r.status_code == 200
    name = stored_name(sha256(data), "notes.txt")          # named after its content, recorded with what it was
    assert (lib / name).read_bytes() == data
    rec = client.get(f"/api/materials/{r.json()['id']}").json()["library"]
    assert (rec["stored_name"], rec["original_name"], rec["sha256"], rec["size"]) == (name, "notes.txt", sha256(data), len(data))
    # the same file again, under another name: refused, naming the book it already is
    again = client.post("/api/materials", files={"file": ("copy of notes.txt", data, "text/plain")})
    assert again.status_code == 409 and again.json()["code"] == "duplicate" and again.json()["material"]["id"] == r.json()["id"]
    other = client.post("/api/materials", files={"file": ("notes.txt", "另一份\n".encode(), "text/plain")})
    assert sorted(p.name for p in lib.iterdir()) == sorted([name, stored_name(sha256("另一份\n".encode()), "notes.txt")])
    # removing a book removes its copy
    client.delete(f"/api/materials/{other.json()['id']}")
    assert [p.name for p in lib.iterdir()] == [name]


def test_copies_from_before_are_renamed_to_content_names(tmp_path):
    from app.db import Database
    from app.library import sha256, stored_name
    from app.materials import MaterialStore
    db, lib = Database(tmp_path / "db.sqlite3"), tmp_path / "library"
    store = MaterialStore(db, files_dir=tmp_path / "files", library_dir=lib)
    data = "舊的講義\n".encode()
    m = store.add("handout.txt", data, None)
    # as an older version left it: no record, the copy under its own name
    with db.conn() as c:
        c.execute("DELETE FROM library_files")
    (lib / stored_name(sha256(data), "handout.txt")).rename(lib / "handout.txt")
    assert MaterialStore(db, files_dir=tmp_path / "files", library_dir=lib).adopt_library() == 1
    assert [p.name for p in lib.iterdir()] == [stored_name(sha256(data), "handout.txt")]
    assert store.get(m["id"])["library"]["original_name"] == "handout.txt"


def test_categories_can_be_reordered(client):
    ids = [client.post("/api/categories", json={"name": n}).json()["id"] for n in ("甲", "乙", "丙")]
    r = client.put("/api/categories/order", json={"ids": [ids[2], ids[0]]})
    assert [c["name"] for c in r.json()] == ["丙", "甲", "乙"]
    assert [c["name"] for c in client.get("/api/library").json()["categories"]] == ["丙", "甲", "乙"]
