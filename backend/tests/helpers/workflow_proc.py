"""Drive a LangGraph session in a separate process (restart tests). argv: <dir> <steps json>.
Steps: {"op": "start"} | {"op": "event", "type", "command_id", "payload"?, "unit": true?} | {"op": "wait"}.
MENTOR_FAULT in the environment hard-exits at that point. Prints one JSON line with the final state."""
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["MENTOR_WORKFLOW"] = "langgraph"

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.db import Database  # noqa: E402
from app.llm.registry import Registry  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import ModelConfig  # noqa: E402

config.WORKFLOW = "langgraph"
config.READING_PREFETCH = False
DIR = Path(sys.argv[1])
STEPS = json.loads(sys.argv[2])
MD = (ROOT / "tests" / "helpers" / "flow.md").read_text(encoding="utf-8")


def wait_idle(c, sid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/sessions/{sid}").json()
        if not (s.get("workflow") or {}).get("active_command"):
            return s
        time.sleep(0.02)
    raise SystemExit("timeout waiting for the runner")


app = create_app(Database(DIR / "db.sqlite3"), Registry(ModelConfig(backend="mock")))
with TestClient(app) as c:
    sid = (DIR / "sid").read_text() if (DIR / "sid").exists() else None
    for st in STEPS:
        if st["op"] == "start":
            m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
            sid = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": len(MD.splitlines())}).json()["id"]
            (DIR / "sid").write_text(sid)
        elif st["op"] == "wait":
            wait_idle(c, sid)
        elif st["op"] == "event":
            s = c.get(f"/api/sessions/{sid}").json()
            body = {"command_id": st["command_id"], "type": st["type"], "payload": st.get("payload", {})}
            r = c.post(f"/api/sessions/{sid}/events", json=body)
            assert r.status_code in (200, 202), r.text
    s = wait_idle(c, sid)
    repo = app.state.repo
    out = {"sid": sid, "revision": s["revision"], "phase": s["state"]["phase"], "index": (s.get("reading") or {}).get("index"),
           "guides": sorted((s.get("reading") or {}).get("guides", {}).keys()),
           "qa": {k: len(v) for k, v in (s.get("reading") or {}).get("qa", {}).items()},
           "read": s["state"].get("reading", {}).get("read"), "turns": len(s["turns"]),
           "commands": {x["command_id"]: x["status"] for x in repo.list_commands(sid)},
           "calls": [(x["node"], x["error_kind"]) for x in repo.list_llm_calls(sid)],
           "waiting": s["workflow"]["waiting"], "segmentation": s["reading"]["segmentation"]["status"] if s.get("reading") else None,
           "concepts": len(s["concepts"])}
    print(json.dumps(out, ensure_ascii=False))
