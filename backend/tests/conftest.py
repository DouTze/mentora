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
