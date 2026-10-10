"""R3 learning state and task contract: generated ≠ presented ≠ read ≠ understood; the reader's declared background
and depth steer the bridges but are recorded as control data, never as subject matter."""
import io

from app import config
from test_workflow import MD, graph_app, idle, prefetch_idle, run  # noqa: F401  (fixture re-export)


def start_with(c, **contract):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    m = m.get("material") or m          # the same file again is the book already imported (409 duplicate)
    s = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": len(MD.splitlines()), **contract}).json()
    return idle(c, s["id"])


def state_of(c, sid):
    return c.app_ref.state.db.get_session(sid)


def draft_prompts(c, sid):
    """Handout-writing requests, oldest first (the trace API hides request bodies; read the repository)."""
    return [x for x in c.app_ref.state.repo.list_llm_calls(sid) if x["node"] == "draft"]


def test_task_contract_records_defaults_and_reader_background(graph_app):
    c = graph_app
    s = start_with(c)
    tc = state_of(c, s["id"])["workflow_meta"]["task_contract"]
    assert tc["depth"] == "close" and set(tc["defaults"]) == {"depth", "goal", "reader_background"}
    s = start_with(c, reading_depth="skim", reader_background="高中生物學過細胞", reading_goal="準備小考")
    tc = state_of(c, s["id"])["workflow_meta"]["task_contract"]
    assert tc["depth"] == "skim" and tc["reader_background"] == "高中生物學過細胞" and tc["defaults"] == []
    prompt = draft_prompts(c, s["id"])[0]["request"]["messages"][0]["content"]
    assert "高中生物學過細胞" in prompt and "略讀" in prompt and "準備小考" in prompt
    guide = state_of(c, s["id"])["state"]["reading"]["guides"]["0"]["guide"]
    assert "高中生物學過細胞" not in guide   # control data never becomes subject matter


def test_generated_presented_read_and_understood_are_different_states(graph_app, monkeypatch):
    c = graph_app
    monkeypatch.setattr(config, "READING_PREFETCH", True)
    s = start_with(c)
    sid = s["id"]
    prefetch_idle(c, sid)
    assert (sid, "written") in c.app_ref.state.runner.prefetch_log          # unit 1 generated ahead of time …
    rd = state_of(c, sid)["state"]["reading"]
    assert rd.get("presented") == [0] and not rd.get("read") and "1" not in rd["guides"]   # … but neither shown nor read
    first = draft_prompts(c, sid)[0]["request"]["messages"][0]["content"]
    assert "這是第一段" in first                                            # unit 0 assumes nothing was read

    s, _ = run(c, sid, "ask", "q1", payload={"question": "光反應發生在哪裡？"})
    s, _ = run(c, sid, "next", "n1")
    rd = state_of(c, sid)["state"]["reading"]
    assert rd["read"] == [0] and rd["presented"] == [0, 1] and not rd.get("evidence")
    # whether unit 1's guide was written on demand or adopted from a prefetch, it was built for the reader as they
    # are now: after the question (a prefetch made before it has another key and is never adopted)
    quotes = state_of(c, sid)["content_snapshot"]["quotes"]
    u1 = quotes[1]["unit_id"]
    for_u1 = [x for x in draft_prompts(c, sid) if u1 in (x["operation_key"] or "")]
    used = for_u1[-1]["request"]["messages"][0]["content"]
    read = state_of(c, sid)["state"]["reading"]["guides"]["0"]["key_points"]
    assert read and read[0] in used and "光反應發生在哪裡" in used         # what was read and asked carries on


def test_guide_language_follows_the_material_setting(graph_app):
    """A material's guides are in its own language unless it asks for 繁體中文; the choice is fixed per session."""
    c = graph_app
    s = start_with(c)
    assert state_of(c, s["id"])["workflow_meta"]["guide_language"] == "source"
    mid = s["source"]["material_id"]
    assert c.patch(f"/api/materials/{mid}", json={"translate_zh": True}).json()["translate_zh"] is True
    s2 = c.post("/api/sessions", json={"material_id": mid, "start": 1, "end": len(MD.splitlines())}).json()
    assert state_of(c, s2["id"])["workflow_meta"]["guide_language"] == "zh-TW"
    assert state_of(c, s["id"])["workflow_meta"]["guide_language"] == "source"   # a started session keeps its own


EN = ("# Markov decision processes\n\nIn this chapter we introduce the formal problem of finite Markov decision "
      "processes, which we try to solve in the rest of the book.\n\nMDPs are a classical formalization of sequential "
      "decision making, where actions influence not just immediate rewards but also subsequent states.\n")


def test_prompts_carry_the_session_language(graph_app):
    """Every request of a session names the language to write in — the one the program reads from the source in
    source mode — and no language field is left unfilled."""
    from app.tutor.reading import ZH_RULE
    c = graph_app
    s = start_with(c)                                                        # a Chinese material, source mode
    mid = s["source"]["material_id"]
    c.patch(f"/api/materials/{mid}", json={"translate_zh": True})
    s2 = c.post("/api/sessions", json={"material_id": mid, "start": 1, "end": len(MD.splitlines())}).json()
    idle(c, s2["id"])
    m = c.post("/api/materials", files={"file": ("mdp.md", io.BytesIO(EN.encode()))}).json()
    s3 = idle(c, c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": len(EN.splitlines())}).json()["id"])
    # s2 writes in the same language as s (繁體中文 either way), so it takes over s's accepted guides
    assert {g.get("reused_from") for g in c.app_ref.state.db.get_session(s2["id"])["state"]["reading"]["guides"].values()} == {s["id"]}
    for sid, rule in ((s["id"], ZH_RULE), (s3["id"], "一律使用英文撰寫")):
        bodies = [m["content"] for x in c.app_ref.state.repo.list_llm_calls(sid) for m in x["request"]["messages"]
                  if isinstance(m.get("content"), str)]
        assert bodies and not any("{{language" in b for b in bodies)
        assert any(rule in b for b in bodies)
    draft = next(x for x in c.app_ref.state.repo.list_llm_calls(s3["id"]) if x["node"] == "draft")
    assert "講義用**英文**撰寫" in draft["request"]["messages"][0]["content"]


def test_the_source_language_is_read_from_its_writing_system():
    from app.tutor.reading import language_fields, output_language, source_language
    assert source_language(EN) == "英文" and source_language(MD) == "中文"
    assert source_language("これは日本語の文章です。") == "日文" and source_language("12345 + 678") is None
    assert source_language("Les processus de décision markoviens sont une formalisation classique.") is None
    assert output_language("source", source_language(EN)) == {"code": "src", "name": "英文"}
    assert language_fields(output_language("source", None))["language"] == "課本原文的語言"
    assert output_language("zh-TW", "英文") == {"code": "zh", "name": "繁體中文"}         # translate_zh wins
    assert output_language("source", "中文")["code"] == "zh"                              # a Chinese material
