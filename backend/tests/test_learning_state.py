"""R3 learning state and task contract: generated ≠ presented ≠ read ≠ understood; the reader's declared background
and depth steer the bridges but are recorded as control data, never as subject matter."""
import io

from app import config
from test_workflow import MD, graph_app, idle, prefetch_idle, run  # noqa: F401  (fixture re-export)


def start_with(c, **contract):
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    s = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": len(MD.splitlines()), **contract}).json()
    return idle(c, s["id"])


def state_of(c, sid):
    return c.app_ref.state.db.get_session(sid)


def bridge_prompts(c, sid):
    """Bridge-planning requests, oldest first (the trace API hides request bodies; read the repository)."""
    return [x for x in c.app_ref.state.repo.list_llm_calls(sid) if x["node"] == "plan_bridges"]


def test_task_contract_records_defaults_and_reader_background(graph_app):
    c = graph_app
    s = start_with(c)
    tc = state_of(c, s["id"])["workflow_meta"]["task_contract"]
    assert tc["depth"] == "close" and set(tc["defaults"]) == {"depth", "goal", "reader_background"}
    s = start_with(c, reading_depth="skim", reader_background="高中生物學過細胞", reading_goal="準備小考")
    tc = state_of(c, s["id"])["workflow_meta"]["task_contract"]
    assert tc["depth"] == "skim" and tc["reader_background"] == "高中生物學過細胞" and tc["defaults"] == []
    prompt = bridge_prompts(c, s["id"])[0]["request"]["messages"][0]["content"]
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
    first = bridge_prompts(c, sid)[0]["request"]["messages"][0]["content"]
    assert "尚未讀過其他段落" in first                                      # unit 0 assumes nothing was read

    s, _ = run(c, sid, "ask", "q1", payload={"question": "光反應發生在哪裡？"})
    s, _ = run(c, sid, "next", "n1")
    rd = state_of(c, sid)["state"]["reading"]
    assert rd["read"] == [0] and rd["presented"] == [0, 1] and not rd.get("evidence")
    # whether unit 1's guide was written on demand or adopted from a prefetch, it was built for the reader as they
    # are now: after the question (a prefetch made before it has another key and is never adopted)
    quotes = state_of(c, sid)["content_snapshot"]["quotes"]
    u0, u1 = quotes[0]["unit_id"], quotes[1]["unit_id"]
    for_u1 = [x for x in bridge_prompts(c, sid) if u1 in (x["operation_key"] or "")]
    used = for_u1[-1]["request"]["messages"][0]["content"]
    assert f"[{u0}]" in used and "已讀不代表已掌握" in used and "光反應發生在哪裡" in used
