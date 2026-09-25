"""End-to-end flow tests using the mock backend (no model required)."""
from conftest import answer, start

CORRECT_1 = "不同意。夏天氣溫高，大家既買冰淇淋也去游泳，所以是共同原因造成的相關。"
CORRECT_1T = "書多的家庭通常家長重視教育，這個第三因素同時造成書多和成績好。"
CORRECT_2 = "真正的原因是年齡：年紀越大腳越大，閱讀能力也越好。"
CORRECT_2T = "火災規模是共同原因，大火同時需要更多消防車也造成更多損失。"


def test_full_pass_without_help(client):
    s = start(client)
    assert s["status"] == "active"
    assert s["current_question"]["id"] == "q-diag-01"
    assert s["model_config_snapshot"]["simulated"] is True

    r = answer(client, s["id"], CORRECT_1)
    assert r["message"]["action"] == "advance"
    assert r["session"]["current_question"]["id"] == "q-transfer-01"

    r = answer(client, s["id"], CORRECT_1T)
    assert r["message"]["action"] == "pass"
    assert r["session"]["current_question"]["id"] == "q-diag-02"

    answer(client, s["id"], CORRECT_2)
    r = answer(client, s["id"], CORRECT_2T)
    assert r["session"]["status"] == "completed"
    summ = r["session"]["summary"]
    assert summ["verdict"] == "passed"
    assert [p["status"] for p in summ["practiced"]] == ["passed", "passed"]
    assert summ["turn_count"] == 4

    # completed sessions refuse further answers
    answer(client, s["id"], "再答一次", expect=409)


def test_wrong_then_hint_then_explain(client):
    s = start(client)
    r = answer(client, s["id"], "同意，吃冰淇淋會抽筋所以溺水。")
    assert r["message"]["action"] == "hint"
    assert r["message"]["hint"]
    assert r["session"]["current_question"]["hints_used"] == 1

    r = answer(client, s["id"], "我不知道")
    assert r["message"]["action"] == "hint"
    assert r["session"]["current_question"]["hints_used"] == 2

    # third failure: hints exhausted → show teaching material, mark needs_review, move on
    r = answer(client, s["id"], "還是不懂")
    assert r["message"]["action"] == "explain"
    assert r["message"]["explanation"]
    assert len(r["message"]["examples"]) >= 1
    st = r["session"]["state"]
    assert st["concepts"]["concept-01"]["status"] == "needs_review"
    assert st["question_id"] == "q-transfer-01"
    assert "q-diag-01" in st["stuck_at"]


def test_correct_conclusion_with_flawed_reasoning_is_probed(client):
    s = start(client)
    r = answer(client, s["id"], "不同意，冰淇淋和溺水根本沒關係。")
    assert r["turn"]["judgement"]["assessment"] == "correct"
    assert r["turn"]["judgement"]["reasoning_ok"] is False
    assert r["message"]["action"] == "probe"
    assert r["message"]["probe"]
    assert r["turn"]["decision"]["rule"] == "correct + reasoning flawed"
    # still on the same question, not marked learned
    assert r["session"]["current_question"]["id"] == "q-diag-01"


def test_unclear_probe_then_falls_back_to_hint(client):
    s = start(client)
    r = answer(client, s["id"], "嗯")
    assert r["message"]["action"] == "probe"
    r = answer(client, s["id"], "嗯嗯")
    assert r["message"]["action"] == "hint"
    assert "probes exhausted" in r["turn"]["decision"]["rule"]


def test_transfer_solved_with_hint_is_passed_with_help(client):
    s = start(client)
    answer(client, s["id"], CORRECT_1)
    answer(client, s["id"], "發書不一定有用吧")  # partial → hint
    r = answer(client, s["id"], CORRECT_1T)
    assert r["message"]["action"] == "pass"
    assert r["session"]["state"]["concepts"]["concept-01"]["status"] == "passed_with_help"


def test_invalid_model_output_is_recorded_and_retryable(client):
    s = start(client)
    r = answer(client, s["id"], CORRECT_1, expect=502, debug_invalid="true")
    assert r["code"] == "invalid_model_output"
    assert r["retryable"] is True
    assert r["turn"]["error"].startswith("invalid_output")
    assert r["turn"]["student_answer"] == CORRECT_1
    # state untouched, retry with valid output succeeds
    v = client.get(f"/api/sessions/{s['id']}").json()
    assert v["current_question"]["id"] == "q-diag-01"
    assert len(v["turns"]) == 1 and v["turns"][0]["error"]
    r = answer(client, s["id"], CORRECT_1)
    assert r["message"]["action"] == "advance"


def test_model_connection_failure_keeps_input_and_does_not_fallback(db_path):
    from fastapi.testclient import TestClient
    from app.db import Database
    from app.llm.registry import Registry
    from app.main import create_app
    from app.models import ModelConfig

    # Ollama pointed at a port nobody listens on.
    reg = Registry(ModelConfig(backend="ollama", host="http://127.0.0.1:9", model="qwen3:8b"))
    with TestClient(create_app(Database(db_path), reg)) as c:
        s = start(c)
        assert s["model_config_snapshot"]["simulated"] is False
        r = answer(c, s["id"], CORRECT_1, expect=502)
        assert r["code"] == "model_unavailable"
        assert r["retryable"] is True
        assert r["turn"]["error"].startswith("connection")
        v = c.get(f"/api/sessions/{s['id']}").json()
        assert v["status"] == "active" and v["current_question"]["id"] == "q-diag-01"
        assert v["turns"][0]["judgement"] is None  # no simulated judgement was substituted


def test_restart_keeps_records_and_resumes(db_path):
    from fastapi.testclient import TestClient
    from app.db import Database
    from app.llm.registry import Registry
    from app.main import create_app
    from app.models import ModelConfig

    with TestClient(create_app(Database(db_path), Registry(ModelConfig(backend="mock")))) as c:
        s = start(c)
        answer(c, s["id"], CORRECT_1)
        sid = s["id"]

    # "restart": new app instance on the same file
    with TestClient(create_app(Database(db_path), Registry(ModelConfig(backend="mock")))) as c:
        v = c.get(f"/api/sessions/{sid}").json()
        assert v["current_question"]["id"] == "q-transfer-01"
        assert len(v["turns"]) == 1
        assert c.get("/api/sessions").json()[0]["id"] == sid
        r = answer(c, sid, CORRECT_1T)
        assert r["message"]["action"] == "pass"


def test_snapshot_survives_content_change(client):
    s = start(client)
    svc = client.app.state.svc
    # simulate someone editing the content file after the session started
    svc.contents["correlation-causation"]["questions"][0]["prompt"] = "CHANGED"
    v = client.get(f"/api/sessions/{s['id']}").json()
    assert v["current_question"]["prompt"] != "CHANGED"


def test_replay_with_current_settings(client):
    s = start(client)
    answer(client, s["id"], CORRECT_1)
    answer(client, s["id"], "我不知道")
    r = client.post(f"/api/sessions/{s['id']}/replay").json()
    assert r["replayed_answers"] == 2 and r["total_answers"] == 2
    assert r["session"]["id"] != s["id"]
    assert [t["student_answer"] for t in r["session"]["turns"]] == [CORRECT_1, "我不知道"]
    assert r["session"]["turns"][1]["decision"]["action"] == "hint"


def test_settings_roundtrip_and_backend_pinning(client):
    s = start(client)
    r = client.put("/api/settings", json={"backend": "ollama", "model": "qwen3:8b", "host": "http://127.0.0.1:9",
                                          "model_path": "", "device": "GPU", "temperature": 0.2,
                                          "max_tokens": 512, "context_tokens": 8192})
    assert r.json()["active"]["backend"] == "ollama"
    # a session started on mock refuses to continue on another backend
    r = answer(client, s["id"], CORRECT_1, expect=409)
    assert r["code"] == "backend_changed"
    chk = client.post("/api/settings/check").json()
    assert chk["health"]["ok"] is False


def test_export_markdown_and_json(client):
    s = start(client)
    answer(client, s["id"], CORRECT_1)
    md = client.get(f"/api/sessions/{s['id']}/export", params={"format": "md"})
    assert md.status_code == 200 and "# 學習紀錄" in md.text and "模擬" in md.text and "advance" in md.text
    js = client.get(f"/api/sessions/{s['id']}/export", params={"format": "json"}).json()
    assert js["turns"][0]["model_request"]["messages"][0]["role"] == "user"
    assert "評分依據" in js["turns"][0]["model_request"]["messages"][0]["content"]


def test_parse_judgement_tolerates_think_and_fences():
    from app.tutor.session import parse_judgement
    raw = '<think>hmm</think>\n```json\n{"assessment":"partial","feedback":"ok","source_ids":["concept-01","bogus"]}\n```'
    j, notes = parse_judgement(raw, {"concept-01"})
    assert j.assessment == "partial" and j.source_ids == ["concept-01"]
    assert any("bogus" in n for n in notes)


def test_companions_apply_active_model(client):
    assert client.get("/api/companions").json() is None
    base = {"model": "", "host": "", "model_path": "", "device": "GPU", "temperature": 0.2, "max_tokens": 512, "context_tokens": 8192}
    body = {"active": "fox", "companions": {"owl": {**base, "backend": "mock"},
                                            "fox": {**base, "backend": "ollama", "model": "qwen3-vl:2b", "temperature": 0.7}}}
    r = client.put("/api/companions", json=body).json()
    assert r["active"]["backend"] == "ollama" and r["config"]["temperature"] == 0.7
    assert client.get("/api/settings").json()["config"]["model"] == "qwen3-vl:2b"
    assert client.get("/api/companions").json()["active"] == "fox"
    assert client.put("/api/companions", json={**body, "active": "nobody"}).status_code == 400
