"""Bookmarks: a page marked with its chapter, listed for every book in page order, and gone with the book."""
import io


def test_bookmarks_are_kept_per_page_with_their_chapter_and_removed_with_the_book(client):
    text = "\n".join(f"第 {i} 行" for i in range(1, 11))
    m = client.post("/api/materials", files={"file": ("bm.txt", io.BytesIO(text.encode()))}).json()
    mid = m["id"]
    assert client.put(f"/api/materials/{mid}/bookmarks/7", json={"chapter": "二、方法"}).status_code == 200
    r = client.put(f"/api/materials/{mid}/bookmarks/3", json={})
    assert [b["unit_no"] for b in r.json()] == [3, 7]
    # marking the same page again only renames its chapter
    client.put(f"/api/materials/{mid}/bookmarks/7", json={"chapter": "二、方法與資料"})
    marks = client.get("/api/bookmarks").json()[mid]
    assert [(b["unit_no"], b["chapter"]) for b in marks] == [(3, None), (7, "二、方法與資料")]
    assert client.put(f"/api/materials/{mid}/bookmarks/99", json={}).status_code == 422
    assert [b["unit_no"] for b in client.delete(f"/api/materials/{mid}/bookmarks/3").json()] == [7]
    client.delete(f"/api/materials/{mid}")
    assert mid not in client.get("/api/bookmarks").json()


def test_bookmarks_and_guided_readings_take_names_of_their_own(client):
    m = client.post("/api/materials", files={"file": ("named.txt", io.BytesIO("一\n二\n三\n".encode()))}).json()
    client.put(f"/api/materials/{m['id']}/bookmarks/2", json={"chapter": "第一章"})
    marks = client.patch(f"/api/materials/{m['id']}/bookmarks/2", json={"label": "  要複習的定義 "}).json()
    assert marks[0]["label"] == "要複習的定義" and marks[0]["chapter"] == "第一章"   # the chapter is kept
    assert client.patch(f"/api/materials/{m['id']}/bookmarks/2", json={"label": ""}).json()[0]["label"] is None
    assert client.put("/api/labels/session/s1", json={"label": "第一次讀"}).json() == {"s1": "第一次讀"}
    assert client.get("/api/labels/session").json() == {"s1": "第一次讀"}
    assert client.put("/api/labels/session/s1", json={"label": " "}).json() == {}      # back to its range
    assert client.get("/api/labels/other").status_code == 404
