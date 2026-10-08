"""The book folder: content names, the record of what each copy stands for, duplicates refused, copies removed with
their book, and the description and alias the connected model writes."""
import asyncio
from types import SimpleNamespace

from app.db import Database
from app.library import sha256, stored_name
from app.library_describe import BookNote, LibraryDescriber
from app.materials import MaterialError, MaterialStore


def store(tmp_path):
    return MaterialStore(Database(tmp_path / "db.sqlite3"), files_dir=tmp_path / "files", library_dir=tmp_path / "lib")


def test_duplicate_file_is_refused_with_the_book_it_already_is(tmp_path):
    s = store(tmp_path)
    data = "# 線性代數\n\n向量空間的定義。\n".encode()
    m = s.add("linear algebra.md", data, None, original_path="/Users/x/Books/linear algebra.md")
    try:
        s.add("renamed.md", data, None)
        raise AssertionError("a duplicate was imported")
    except MaterialError as e:
        assert e.code == "duplicate" and e.extra["material"]["id"] == m["id"]
    rec = s.get(m["id"])["library"]
    assert rec["original_path"] == "/Users/x/Books/linear algebra.md" and rec["stored_name"] == stored_name(sha256(data), "x.md")
    assert len(s.list()) == 1


def test_removing_a_book_keeps_a_copy_another_record_still_uses(tmp_path):
    s = store(tmp_path)
    data = "共用的檔案\n".encode()
    a = s.add("a.txt", data, None)
    # two materials of one file, as imports from before duplicates were refused can be
    b = s.add("b.txt", "暫時不同\n".encode(), None)
    with s.db.conn() as c:
        c.execute("UPDATE library_files SET stored_name=(SELECT stored_name FROM library_files WHERE material_id=?) "
                  "WHERE material_id=?", (a["id"], b["id"]))
    s.delete(b["id"])
    assert (s.library_dir / stored_name(sha256(data), "a.txt")).exists()
    s.delete(a["id"])
    assert not (s.library_dir / stored_name(sha256(data), "a.txt")).exists()   # the last record of it: removed


class Backend:
    simulated = False

    def describe(self):
        return {"backend": "ollama", "model": "qwen3:8b", "simulated": False}


def test_the_model_writes_a_description_and_an_alias(tmp_path):
    s = store(tmp_path)
    m = s.add("Sutton_Barto_RL_2nd_2018.txt", "Reinforcement Learning: An Introduction\nChapter 1\n".encode(), None,
              original_path="/Users/x/Downloads/Sutton_Barto_RL_2nd_2018.txt")
    d = LibraryDescriber(SimpleNamespace(backend=Backend(), cfg=None), None, s)
    seen = []

    async def ask(prompt):
        seen.append(prompt)
        return BookNote(description="強化學習的經典入門教科書，由 Sutton 與 Barto 撰寫。", alias="Sutton 強化學習")

    d._ask = ask
    assert asyncio.run(d.describe(m["id"])) == {"alias": "Sutton 強化學習", "description": "強化學習的經典入門教科書，由 Sutton 與 Barto 撰寫。"}
    assert "Sutton_Barto_RL_2nd_2018.txt" in seen[0] and "/Users/x/Downloads" in seen[0] and "Chapter 1" in seen[0]
    rec = s.get(m["id"])["library"]
    assert rec["alias"] == "Sutton 強化學習" and rec["described_by"] == "ollama:qwen3:8b" and rec["described_at"]
    assert s.library.undescribed() == []
    assert s.list()[0]["alias"] == "Sutton 強化學習"


def test_nothing_is_written_without_a_real_model(tmp_path):
    s = store(tmp_path)
    s.add("notes.txt", "筆記\n".encode(), None)
    d = LibraryDescriber(SimpleNamespace(backend=SimpleNamespace(simulated=True), cfg=None), None, s)
    assert not d.available()
    d.schedule("whatever")
    d.sweep()
    assert d._sweeping is None and not d._tasks


def test_a_book_imported_from_a_worker_thread_is_described_on_the_apps_loop(tmp_path):
    """The shelf import is a sync endpoint (run in a worker thread, no event loop there): scheduling its description
    must hand the work to the app's loop instead of failing with "no running event loop"."""
    import threading

    s = store(tmp_path)
    m = s.add("book.txt", "某本書\n".encode(), None)
    d = LibraryDescriber(SimpleNamespace(backend=Backend(), cfg=None), None, s)
    done = []

    async def describe(mid):
        done.append(mid)
    d.describe = describe

    async def app():
        d.sweep()   # at start, on the loop: the loop is remembered
        errors = []

        def import_in_thread():
            try:
                d.schedule(m["id"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        t = threading.Thread(target=import_in_thread)
        t.start()
        t.join()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if done.count(m["id"]) >= 2:
                break
        return errors

    assert asyncio.run(app()) == []
    assert done.count(m["id"]) == 2   # once by the start sweep, once for the import
