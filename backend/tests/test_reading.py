"""導讀 before the quiz: quote splitting, guide / ask / next, part compaction, then questions (mock backend)."""
import io

from app.tutor import reading
from conftest import answer

MD = """# 光合作用

植物利用光能把二氧化碳和水轉換成葡萄糖與氧氣，這個過程叫做光合作用。它發生在葉綠體中，需要光、水與二氧化碳三個條件同時具備才能進行。

## 光反應

光反應發生在類囊體膜上。葉綠素吸收光能後把水分解，釋放出氧氣，並產生 ATP 與 NADPH 供後續反應使用。這一步的關鍵是光能被轉換成化學能儲存起來，沒有光就無法進行。

## 暗反應

暗反應不直接需要光，發生在葉綠體的基質中。它利用光反應產生的 ATP 與 NADPH，把二氧化碳固定成葡萄糖，這個循環稱為卡爾文循環，是植物累積有機物的主要途徑。

小結

光反應提供能量，暗反應製造糖。
"""


def wait(client, sid, ready, tries=200):
    """Poll the session until `ready(session)` is true (background jobs finish in ms with the mock)."""
    import time
    for _ in range(tries):
        s = client.get(f"/api/sessions/{sid}").json()
        if ready(s):
            return s
        job = (s.get("reading") or {}).get("job")
        assert not (job and job["error"]), job
        time.sleep(0.01)
    raise AssertionError(f"timed out; last job={(s.get('reading') or {}).get('job')}")


def segmented(s):
    return (s["reading"]["segmentation"] or {}).get("status") == "done"


def guide_ready(client, sid):
    """Request the current guide and wait for it."""
    r = client.post(f"/api/sessions/{sid}/reading/guide")
    assert r.status_code == 200, r.text
    s = wait(client, sid, lambda s: segmented(s) and str(s["reading"]["index"]) in s["reading"]["guides"])
    if r.json()["status"] == "pending":  # the guide arrived through a job; ask once more so the response carries it
        r = client.post(f"/api/sessions/{sid}/reading/guide")
    return r.json()["guide"], s


def next_ready(client, sid):
    before = client.get(f"/api/sessions/{sid}").json()["reading"]["index"]
    r = client.post(f"/api/sessions/{sid}/reading/next")
    assert r.status_code == 200, r.text
    return wait(client, sid, lambda s: s["state"]["phase"] != "reading" or (s["reading"]["index"] > before and not s["reading"]["job"]))


def upload(client, name, data, category_id=None):
    form = {"category_id": category_id} if category_id else {}
    r = client.post("/api/materials", files={"file": (name, io.BytesIO(data))}, data=form)
    assert r.status_code == 200, r.text
    return r.json()


# -- splitting (pure) ---------------------------------------------------------

def test_split_lines_one_paragraph_per_quote_and_tiny_lines_merge():
    units = MD.split("\n")
    quotes = reading.split_quotes(units, "line", 1)
    labels = [(q["heading"], q["label"]) for q in quotes]
    # one original paragraph = one quote; the heading attaches to the paragraph below it;
    # only the tiny 小結 line + its one-liner (both under QUOTE_MIN_CHARS) merge into the previous quote
    assert labels[0] == ("光合作用", "第 3 行")
    assert labels[1] == ("光反應", "第 7 行")
    assert quotes[2]["heading"] == "暗反應" and "小結" in quotes[2]["text"] and "光反應提供能量" in quotes[2]["text"]
    assert len(quotes) == 3
    assert all(q["excerpt"] and q["chars"] == len(q["text"]) for q in quotes)


def test_split_pages_and_oversize_paragraph_is_cut_at_sentence_ends():
    long_para = "這是一個很長的句子，用來測試切割。" * 120  # ~2000 chars without blank lines
    pages = ["第一頁只有一段短文，講一件事。\n\n第二段講另一件事，而且長度足夠自成一段，所以這裡再多寫一些文字讓它超過最小長度的門檻，這樣就不會和前一段合併在一起，而是各自成為獨立的來源單元。",
             long_para]
    quotes = reading.split_quotes(pages, "page", 4)
    # the short first paragraph merged with the second (both on page 4)
    assert "第二段" in quotes[0]["text"] and quotes[0]["label"] == "第 4 頁 第 1–2 段"
    cut = [q for q in quotes if q["label"].startswith("第 5 頁")]
    # pieces are about QUOTE_TARGET_CHARS, never over the cap
    assert len(cut) >= 5 and all(q["chars"] <= reading.QUOTE_MAX_CHARS for q in cut)
    assert max(q["chars"] for q in cut) <= reading.QUOTE_TARGET_CHARS + 20
    assert all(q["text"].endswith("。") for q in cut)  # never mid-sentence
    assert cut[0]["label"] == "第 5 頁（之 1）"


def test_plan_parts_groups_contiguous_quotes():
    quotes = [{"index": i, "label": f"第 {i} 行", "chars": 3000} for i in range(5)]
    parts = reading.plan_parts(quotes, part_chars=5000)
    assert [(p["quote_from"], p["quote_to"]) for p in parts] == [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]
    quotes = [{"index": i, "label": f"第 {i} 行", "chars": 2000} for i in range(5)]
    parts = reading.plan_parts(quotes, part_chars=5000)
    assert [(p["quote_from"], p["quote_to"]) for p in parts] == [(0, 1), (2, 3), (4, 4)]
    assert [q["part"] for q in quotes] == [0, 0, 1, 1, 2]
    assert parts[0]["label"] == "第 0 行 至 第 1 行"


# -- flow (mock backend) ------------------------------------------------------

def test_reading_then_questions(client, db_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "READING_PREFETCH", False)   # guides only on request, so "not yet read" states are observable
    m = upload(client, "photo.md", MD.encode())
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 15}).json()
    assert s["state"]["phase"] == "reading" and s["current_question"] is None and s["concepts"] == []
    rd = s["reading"]
    assert rd["total"] == 3 and rd["index"] == 0 and rd["done"] is False and rd["guides"] == {}
    assert rd["quotes"][0]["heading"] == "光合作用"
    # the workspace exists on disk: one file per quote
    ws = db_path.parent / "reading" / s["id"]
    assert (ws / "manifest.json").exists() and (ws / "quotes" / "01.md").read_text().startswith("# 第 3 行 · 光合作用")

    # answering is refused until the reading is done
    answer(client, s["id"], "隨便答", expect=409)
    # cannot move on before the guide has been shown
    r = client.post(f"/api/sessions/{s['id']}/reading/next")
    assert r.status_code == 400 and r.json()["code"] == "guide_missing"

    g, sv = guide_ready(client, s["id"])
    assert g["guide"].startswith("（模擬導讀）") and g["key_points"] and g["check"]
    assert sv["reading"]["guides"]["0"]["guide"] == g["guide"] and sv["reading"]["job"] is None
    assert (ws / "guides" / "01.md").exists()
    # asking again returns the cached guide at once
    r = client.post(f"/api/sessions/{s['id']}/reading/guide").json()
    assert r["status"] == "ready" and r["guide"]["guide"] == g["guide"]

    r = client.post(f"/api/sessions/{s['id']}/reading/ask", json={"question": "葉綠體是什麼？"})
    assert r.status_code == 200, r.text
    assert "葉綠體" in r.json()["answer"]["answer"] and r.json()["session"]["reading"]["qa"]["0"][0]["question"] == "葉綠體是什麼？"

    sv = next_ready(client, s["id"])
    assert sv["reading"]["index"] == 1
    guide_ready(client, s["id"])
    next_ready(client, s["id"])
    guide_ready(client, s["id"])
    sv = next_ready(client, s["id"])
    assert sv["reading"]["done"] is True and sv["reading"]["quiz_source"] == "full" and sv["reading"]["job"] is None
    assert sv["state"]["phase"] == "diagnostic" and sv["current_question"]["type"] == "diagnostic" and sv["concepts"]
    # the questions asked are part of the record
    assert sv["questions"] and sv["questions"][0]["prompt"] == sv["current_question"]["prompt"]
    # what the student asked while reading reaches the question writer (the mock echoes it into the first question)
    assert "葉綠體是什麼？" in sv["current_question"]["prompt"]
    # a single part is never compacted (the full text fits the question writer)
    assert sv["reading"]["compacts"] == {}
    # reading endpoints are closed now, answering works
    assert client.post(f"/api/sessions/{s['id']}/reading/next").status_code == 409
    r = answer(client, s["id"], "我不知道")
    assert r["message"]["action"] == "hint"

    md = client.get(f"/api/sessions/{s['id']}/export", params={"format": "md"}).text
    assert "## 導讀" in md and "葉綠體是什麼？" in md and "（模擬導讀）" in md
    lst = client.get("/api/sessions").json()[0]
    assert lst["source"]["label"] == "第 1–15 行"


def test_long_range_is_read_in_parts_and_quiz_uses_compact_notes(client, monkeypatch):
    monkeypatch.setattr(reading, "PART_CHARS", 120)       # tiny parts so a small file needs several
    monkeypatch.setattr(reading, "GEN_TEXT_BUDGET", 100)  # and the range counts as "too long"
    m = upload(client, "photo.md", MD.encode())
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 15}).json()
    parts = s["reading"]["parts"]
    assert len(parts) >= 2 and parts[0]["quote_from"] == 0
    for _ in range(s["reading"]["total"]):
        guide_ready(client, s["id"])
        sv = next_ready(client, s["id"])
    assert sv["state"]["phase"] == "diagnostic"
    assert sv["reading"]["quiz_source"] == "compact"
    assert set(sv["reading"]["compacts"]) == {str(p["index"]) for p in parts}
    c = sv["reading"]["compacts"]["0"]
    assert c["summary"].startswith("（模擬精簡筆記）") and c["markdown"].startswith("# 精簡筆記：") and "model_raw" not in c


def test_replay_of_a_reading_session_is_refused_until_it_has_questions(client):
    m = upload(client, "photo.md", MD.encode())
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 5}).json()
    r = client.post(f"/api/sessions/{s['id']}/replay")
    assert r.status_code == 409 and r.json()["code"] == "reading"


def test_reading_log_feeds_the_question_prompt():
    quotes = [{"index": 0, "label": "第 3 行", "heading": "光合作用"}, {"index": 1, "label": "第 7 行", "heading": None}]
    rd = {"guides": {"0": {"key_points": ["原料與產物"]}}, "qa": {"1": [{"question": "ATP 是什麼？", "answer": "能量分子。"}]}}
    log = reading.reading_log(quotes, rd)
    assert "第 1 段（第 3 行，光合作用）" in log and "- 重點：原料與產物" in log
    assert "第 2 段（第 7 行）" in log and "學生問：ATP 是什麼？" in log and "導師答：能量分子。" in log
    from app.tutor import generate
    msgs = generate.build_messages({"title": "光合作用", "unit": "line"}, 1, 15, "原文", reading_log=log)
    assert "學生問：ATP 是什麼？" in msgs[0]["content"] and "優先涵蓋" in msgs[0]["content"]
    assert reading.reading_log(quotes, {"guides": {}, "qa": {}}) == "（學生在導讀時沒有提問）"


def test_two_real_paragraphs_are_never_merged_even_when_short():
    units = ["機器學習是讓電腦從資料中學習的科學，也是一門藝術。這裡同時使用「科學」與「藝術」，是在提醒讀者它有兩個面向。", "",
             "Arthur Samuel 在 1959 年提出更廣義的說法：讓電腦獲得學習能力，而不必由人把每一種情況的行為都寫成規則。", "",
             "接著看更工程化的定義。"]
    quotes = reading.split_quotes(units, "line", 1)
    # the first two paragraphs each carry a full thought (≥ QUOTE_MIN_CHARS) → separate quotes;
    # the trailing transition line is too short to teach on its own → joins the previous one
    assert [q["label"] for q in quotes] == ["第 1 行", "第 3–5 行"]
    assert "接著看" in quotes[1]["text"]


def test_non_chinese_source_requires_chinese_text():
    en = {"index": 0, "label": "第 1 行", "heading": None,
          "text": "Machine learning is the science of programming computers so they can learn from data."}
    zh = {"index": 0, "label": "第 1 行", "heading": None, "text": "機器學習是讓電腦從資料中學習的科學。"}
    assert not reading.is_chinese(en["text"]) and reading.is_chinese(zh["text"])
    assert "必填" in reading.guide_messages("t", "r", en, 1, [])[0]["content"]
    assert "填 null" in reading.guide_messages("t", "r", zh, 1, [])[0]["content"]
    ok = reading.Guide(chinese_text="機器學習是……", guide="機器學習是讓電腦從資料中學習的科學。")
    reading.check_guide(en, ok)
    reading.check_guide(zh, reading.Guide(chinese_text=None, guide="機器學習是讓電腦從資料中學習的科學。"))
    import pytest
    with pytest.raises(reading.InvalidReadingOutput):
        reading.check_guide(en, reading.Guide(chinese_text=None, guide="機器學習是讓電腦從資料中學習的科學。"))


def test_english_material_gets_a_chinese_text_in_the_guide(client):
    m = upload(client, "ml.md", b"Machine learning is the science of programming computers so they can learn from data.\n")
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 1}).json()
    g, _ = guide_ready(client, s["id"])
    assert g["chinese_text"] and g["chinese_text"].startswith("（模擬中文課文）")


# -- model segmentation -------------------------------------------------------

def test_apply_segmentation_merges_in_order_and_rejects_bad_plans():
    import pytest
    atoms = reading.split_atoms(MD.split("\n"), "line", 1)
    assert len(atoms) == 3
    seg = reading.Segmentation(units=[{"atoms": [1]}, {"atoms": [2, 3], "title": "光反應與暗反應"}])
    merged = reading.apply_segmentation(atoms, seg)
    assert len(merged) == 2 and merged[1]["heading"] == "光反應" and "暗反應" in merged[1]["text"]
    quotes = reading.finish_quotes("line", merged)
    assert quotes[1]["label"] == "第 7–15 行"
    for bad in ([{"atoms": [1, 3]}, {"atoms": [2]}],      # not contiguous
                [{"atoms": [2]}, {"atoms": [1]}, {"atoms": [3]}],  # out of order
                [{"atoms": [1]}, {"atoms": [2]}],          # atom 3 unused
                [{"atoms": [1]}, {"atoms": [2, 3, 4]}]):   # beyond the candidates
        with pytest.raises(reading.InvalidReadingOutput):
            reading.apply_segmentation(atoms, reading.Segmentation(units=bad))


def test_plan_segments_chunks_by_size():
    atoms = [{"chars": 300} for _ in range(7)]
    assert reading.plan_segments(atoms, chunk_chars=1000) == [(0, 3), (3, 6), (6, 7)]
    assert reading.plan_segments(atoms, chunk_chars=5000) == [(0, 7)]


def test_session_uses_model_segmentation_and_falls_back_to_rules(client, monkeypatch):
    from app.tutor import reading_agent
    m = upload(client, "photo.md", MD.encode())
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 15}).json()
    s = wait(client, s["id"], segmented)
    seg = s["reading"]["segmentation"]
    assert seg["planner"] == "model" and seg["candidates"] == 3 and seg["notes"] == []
    assert s["reading"]["total"] == 3   # the mock keeps one unit per paragraph here

    async def broken(self, title, atoms):
        raise reading_agent.ReadingAgentError("invalid_model_output", "模型輸出格式無效：x")
    monkeypatch.setattr(reading_agent.InProcessReadingAgent, "segment", broken)
    s2 = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 15}).json()
    s2 = wait(client, s2["id"], segmented)
    assert s2["reading"]["total"] == 3 and s2["reading"]["segmentation"]["notes"] and "規則切法" in s2["reading"]["segmentation"]["notes"][0]


# -- Q&A history across quotes -------------------------------------------------

def test_ask_history_spans_earlier_quotes_and_is_labelled():
    quotes = [{"index": i, "label": f"第 {i + 1} 行"} for i in range(3)]
    rd = {"qa": {"0": [{"question": "q0", "answer": "a0"}], "1": [{"question": "q1a", "answer": "a1a"}, {"question": "q1b", "answer": "a1b"}],
                 "2": [{"question": "q2", "answer": "a2"}]}}
    h = reading.ask_history(quotes, rd, 2)
    assert [x["question"] for x in h] == ["q0", "q1a", "q1b", "q2"]
    assert h[0]["quote_index"] == 0 and h[-1]["quote_index"] == 2
    txt = reading.format_history(h, 2)
    assert "［第 1 段（第 1 行）］學生：q0" in txt and "［這一段］學生：q2" in txt
    # earlier-quote exchanges are capped, the current quote's never are
    many = {"qa": {"0": [{"question": f"q{i}", "answer": "a"} for i in range(20)], "2": [{"question": "cur", "answer": "a"}]}}
    h = reading.ask_history(quotes, many, 2)
    assert len(h) == reading.ASK_HISTORY_LIMIT + 1 and h[-1]["question"] == "cur"


def test_ask_prompt_carries_earlier_questions(client):
    m = upload(client, "photo.md", MD.encode())
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 15}).json()
    sid = s["id"]
    guide_ready(client, sid)
    client.post(f"/api/sessions/{sid}/reading/ask", json={"question": "葉綠體是什麼？"})
    next_ready(client, sid)
    guide_ready(client, sid)
    captured = {}
    from app.llm import mock as mock_mod
    orig = mock_mod.MockBackend.generate

    async def spy(self, messages, **kw):
        if (kw.get("meta") or {}).get("task") == "ask":
            captured["prompt"] = messages[0]["content"]
        return await orig(self, messages, **kw)
    mock_mod.MockBackend.generate = spy
    try:
        r = client.post(f"/api/sessions/{sid}/reading/ask", json={"question": "ATP 是什麼？"})
    finally:
        mock_mod.MockBackend.generate = orig
    assert r.status_code == 200
    assert "［第 1 段（第 3 行）］學生：葉綠體是什麼？" in captured["prompt"]


def test_finish_early_writes_questions_from_the_quotes_read(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "READING_PREFETCH", False)   # guides only on request, so "not yet read" states are observable
    monkeypatch.setattr(reading, "PART_CHARS", 120)
    monkeypatch.setattr(reading, "GEN_TEXT_BUDGET", 100)
    m = upload(client, "photo.md", MD.encode())
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 15}).json()
    sid = s["id"]
    # cannot finish before the first guide has been read
    r = client.post(f"/api/sessions/{sid}/reading/finish")
    assert r.status_code == 400 and r.json()["code"] == "guide_missing"
    guide_ready(client, sid)
    next_ready(client, sid)
    guide_ready(client, sid)            # reading quote 2 of 3, then stop
    r = client.post(f"/api/sessions/{sid}/reading/finish")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    sv = wait(client, sid, lambda s: s["state"]["phase"] != "reading")
    rd = sv["reading"]
    assert rd["done"] is True and rd["stopped_at"] == 1 and rd["index"] == 1
    assert sv["current_question"] and sv["reading"]["quiz_source"] == "compact"
    # only the parts that were (partly) read got compacted; the unread quote is not in any note
    assert set(rd["compacts"]) == {str(p["index"]) for p in rd["parts"] if p["quote_from"] <= 1}
    assert all("卡爾文循環" not in c["summary"] for c in rd["compacts"].values())
    md = client.get(f"/api/sessions/{sid}/export", params={"format": "md"}).text
    assert "讀到第 2 段就提前出題" in md


def test_guide_failure_is_reported_in_the_job_and_can_be_retried(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "READING_PREFETCH", False)   # guides only on request, so "not yet read" states are observable
    from app.tutor import reading_agent
    m = upload(client, "photo.md", MD.encode())
    s = client.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 15}).json()
    sid = s["id"]
    wait(client, sid, segmented)
    orig = reading_agent.InProcessReadingAgent.guide
    calls = {"n": 0}

    async def flaky(self, ws, content, quote, prev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise reading_agent.ReadingAgentError("model_unavailable", "Ollama 連線失敗")
        return await orig(self, ws, content, quote, prev)
    monkeypatch.setattr(reading_agent.InProcessReadingAgent, "guide", flaky)
    r = client.post(f"/api/sessions/{sid}/reading/guide").json()
    assert r["status"] == "pending"
    import time
    for _ in range(200):
        sv = client.get(f"/api/sessions/{sid}").json()
        if sv["reading"]["job"] and sv["reading"]["job"]["error"]:
            break
        time.sleep(0.01)
    assert sv["reading"]["job"] == {"stage": "guide", "error": {"code": "model_unavailable", "detail": "Ollama 連線失敗"}}
    g, sv = guide_ready(client, sid)   # calling again retries
    assert g["guide"].startswith("（模擬導讀）") and sv["reading"]["job"] is None


def test_pdf_pages_keep_paragraphs_and_drop_running_headers(client):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((40, 20), "12    Chapter 1. Introduction", fontsize=8)           # running header
    page.insert_textbox(pymupdf.Rect(40, 60, 360, 200), "Machine learning is the science of programming computers so they can learn from data. It is also an art, because real projects need judge-\nment about data quality and constraints.", fontsize=11)
    page.insert_textbox(pymupdf.Rect(40, 220, 360, 330), "A spam filter is a machine learning program. The examples it learns from are called the training set.", fontsize=11)
    data = doc.tobytes()
    m = upload(client, "book.pdf", data)
    units = client.get(f"/api/materials/{m['id']}").json()["units"]
    assert m["unit"] == "page" and len(units) == 1
    paras = units[0].split("\n\n")
    assert len(paras) == 2, units[0]
    assert paras[0].startswith("Machine learning") and "judgement" in paras[0] and "judge- ment" not in paras[0]
    assert "Chapter 1" not in units[0]


def test_excerpt_never_cuts_inside_a_formula():
    text = "整體反應可以寫成 $6\\mathrm{CO_2} + 6\\mathrm{H_2O} \\to \\mathrm{C_6H_{12}O_6} + 6\\mathrm{O_2}$，這是光合作用的總反應式，之後會分成光反應與暗反應兩個階段來談。"
    ex = reading._excerpt(text)
    assert ex.count("$") % 2 == 0 and "…" in ex
    assert reading._excerpt("短句。") == "短句。"
