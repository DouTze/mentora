"""Import → categorise → pick a range → learn, all on the mock backend."""
import io

from pypdf import PdfWriter

from conftest import answer

MD = "# 光合作用\n\n植物利用陽光、水和二氧化碳製造葡萄糖，並釋放氧氣。\n\n葉綠體是進行光合作用的地方，葉綠素負責吸收陽光。\n\n# 呼吸作用\n\n細胞分解葡萄糖取得能量，並產生二氧化碳和水。\n"


def upload(client, name, data, category_id=None, expect=200):
    r = client.post("/api/materials", files={"file": (name, data)}, data={"category_id": category_id} if category_id else {})
    assert r.status_code == expect, r.text
    return r.json()


def test_import_categorise_and_move(client):
    cat = client.post("/api/categories", json={"name": "生物"}).json()
    m = upload(client, "光合作用.md", MD.encode(), cat["id"])
    assert (m["kind"], m["unit"], m["unit_count"], m["title"]) == ("md", "line", 9, "光合作用")
    lib = client.get("/api/library").json()
    assert lib["categories"][0]["name"] == "生物" and lib["materials"][0]["category_id"] == cat["id"]
    assert client.get(f"/api/materials/{m['id']}").json()["units"][0] == "# 光合作用"

    client.patch(f"/api/categories/{cat['id']}", json={"name": "自然"})
    client.patch(f"/api/materials/{m['id']}", json={"title": "光合與呼吸"})  # title only: category untouched
    got = client.get("/api/library").json()
    assert got["categories"][0]["name"] == "自然" and got["materials"][0]["category_id"] == cat["id"]

    client.delete(f"/api/categories/{cat['id']}")  # materials survive, uncategorised
    got = client.get("/api/library").json()
    assert got["categories"] == [] and got["materials"][0]["category_id"] is None and got["materials"][0]["title"] == "光合與呼吸"

    assert client.get(f"/api/materials/{m['id']}/file").status_code == 200
    client.delete(f"/api/materials/{m['id']}")
    assert client.get(f"/api/materials/{m['id']}").status_code == 404


def test_rejects_unsupported_and_textless_files(client):
    assert upload(client, "a.docx", b"x", expect=400)["code"] == "unsupported"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    assert upload(client, "scan.pdf", buf.getvalue(), expect=400)["code"] == "no_text"


def test_learn_within_a_line_range(client):
    m = upload(client, "bio.md", MD.encode())
    bad = client.post("/api/sessions", json={"material_id": m["id"], "start": 5, "end": 99})
    assert bad.status_code == 400 and bad.json()["code"] == "bad_range"

    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 5}).json()
    assert s["source"]["label"] == "第 1–5 行" and s["source"]["material_id"] == m["id"]
    assert "呼吸作用" not in s["source"]["text"]  # outside the range
    # the range is read quote by quote first; questions are written once every quote has been understood
    assert s["state"]["phase"] == "reading" and s["current_question"] is None and not s["concepts"]
    from test_reading import guide_ready, next_ready
    for _ in range(s["reading"]["total"]):
        guide_ready(client, s["id"])
        s = next_ready(client, s["id"])
    assert s["current_question"]["type"] == "diagnostic" and s["concepts"]
    assert client.get("/api/sessions").json()[0]["source"]["label"] == "第 1–5 行"

    r = answer(client, s["id"], "我不知道")
    assert r["message"]["action"] == "hint" and r["message"]["hint"]
    # generated questions live only in the snapshot; replay must still work after the material is gone
    client.delete(f"/api/materials/{m['id']}")
    rp = client.post(f"/api/sessions/{s['id']}/replay").json()
    assert rp["replayed_answers"] == 1 and rp["session"]["source"]["label"] == "第 1–5 行"
    md = client.get(f"/api/sessions/{s['id']}/export", params={"format": "md"}).text
    assert "第 1–5 行" in md
