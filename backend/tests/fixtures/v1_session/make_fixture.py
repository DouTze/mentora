"""Run with the PRE-CHANGE code (commit 38e3c11): a LangGraph session left waiting for the reader after guide 0 and
one question; a second session already in the quiz. Writes the business DB and checkpoints for compatibility tests."""
import io, sys, time, shutil
from pathlib import Path
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app import config
from app.db import Database
from app.llm.registry import Registry
from app.main import create_app
from app.models import ModelConfig

out = Path(sys.argv[1])
config.WORKFLOW = "langgraph"
config.READING_PREFETCH = False
tmp = Path(sys.argv[2]); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
config.READING_DIR = tmp / "reading"
MD = Path("tests/helpers/flow.md").read_text(encoding="utf-8")

def idle(c, sid):
    for _ in range(2000):
        s = c.get(f"/api/sessions/{sid}").json()
        if not s["workflow"]["active_command"]:
            return s
        time.sleep(0.01)
    raise SystemExit("not idle")

app = create_app(Database(tmp / "db.sqlite3"), Registry(ModelConfig(backend="mock")))
with TestClient(app) as c:
    m = c.post("/api/materials", files={"file": ("flow.md", io.BytesIO(MD.encode()))}).json()
    n = len(MD.splitlines())
    a = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": n}).json()
    a = idle(c, a["id"])
    c.post(f"/api/sessions/{a['id']}/events", json={"command_id": "q1", "type": "ask", "payload": {"question": "葉綠體是什麼？"}})
    a = idle(c, a["id"])
    b = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": n}).json()
    b = idle(c, b["id"])
    c.post(f"/api/sessions/{b['id']}/events", json={"command_id": "f1", "type": "finish", "payload": {}})
    b = idle(c, b["id"])
    print("waiting", a["id"], a["workflow"]["waiting"], "quiz", b["id"], b["workflow"]["waiting"], b["status"])
    (out / "ids.txt").write_text(f"{a['id']}\n{b['id']}\n")
for f in ("db.sqlite3", "checkpoints.sqlite3"):
    shutil.copy(tmp / f, out / f)
print(sorted(p.name for p in out.iterdir()))
