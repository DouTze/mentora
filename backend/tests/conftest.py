import os
import tempfile

# the home-page shelf must never walk the real home directory or read the real data dir during tests
os.environ["MENTOR_SHELF_WARMUP"] = "0"
os.environ["MENTOR_SHELF_ROOTS"] = ""
os.environ["MENTOR_SHELF_DIR"] = tempfile.mkdtemp(prefix="mentor-shelf-")
os.environ["MENTOR_EXTRACT_IN_BACKGROUND"] = "0"   # tests use a book right after importing it (test_extract turns it on)
os.environ["MENTOR_LIBRARY_DIR"] = tempfile.mkdtemp(prefix="mentor-library-")   # imports copy files here

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.llm.registry import Registry
from app.main import create_app
from app.models import ModelConfig


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.sqlite3"


@pytest.fixture
def client(db_path):
    app = create_app(Database(db_path), Registry(ModelConfig(backend="mock")))
    with TestClient(app) as c:
        yield c


def start(client, content_id="correlation-causation", strategy_id="rules-v1"):
    r = client.post("/api/sessions", json={"content_id": content_id, "strategy_id": strategy_id})
    assert r.status_code == 200, r.text
    return r.json()


def answer(client, sid, text, expect=200, **params):
    r = client.post(f"/api/sessions/{sid}/answer", json={"answer": text}, params=params)
    assert r.status_code == expect, r.text
    return r.json()


def child_env(**extra: str) -> dict[str, str]:
    """A minimal environment for a test subprocess: a short PATH plus what the OS needs to start Python at all
    (Windows cannot initialise its socket layer — asyncio's _overlapped — without SYSTEMROOT), UTF-8 I/O."""
    keep = ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA")
    env = {"PATH": os.environ.get("PATH", "") if os.name == "nt" else "/usr/bin:/bin", "PYTHONUTF8": "1",
           "PYTHONIOENCODING": "utf-8"}
    return env | {k: os.environ[k] for k in keep if k in os.environ} | extra
