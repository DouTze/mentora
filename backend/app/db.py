"""SQLite persistence. Sessions keep a full snapshot of content/strategy/model config
so old records stay readable after the files change."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,
  content_snapshot TEXT NOT NULL,
  strategy_snapshot TEXT NOT NULL,
  model_config TEXT NOT NULL,
  state TEXT NOT NULL,
  summary TEXT
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  idx INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  concept_id TEXT,
  question_id TEXT,
  phase TEXT,
  student_answer TEXT NOT NULL,
  model_request TEXT,
  model_raw TEXT,
  judgement TEXT,
  validation_notes TEXT,
  decision TEXT,
  tutor_message TEXT,
  state_before TEXT,
  state_after TEXT,
  latency_ms INTEGER,
  usage TEXT,
  error TEXT,
  backend TEXT,
  model TEXT
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, idx);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Database:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        if self._mem is not None:
            self._mem.row_factory = sqlite3.Row
            yield self._mem
            self._mem.commit()
            return
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # -- sessions -----------------------------------------------------------
    def create_session(self, content: dict, strategy: dict, model_config: dict, state: dict) -> dict:
        sid, ts = new_id(), now_iso()
        with self.conn() as c:
            c.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,NULL)",
                (sid, ts, ts, "active", json.dumps(content, ensure_ascii=False),
                 json.dumps(strategy, ensure_ascii=False), json.dumps(model_config, ensure_ascii=False),
                 json.dumps(state, ensure_ascii=False)),
            )
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, sid: str) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return self._row_session(row) if row else None

    def list_sessions(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [self._row_session(r) for r in rows]

    def update_session(self, sid: str, *, state: dict, status: str, summary: dict | None) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE sessions SET state=?, status=?, summary=?, updated_at=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), status,
                 json.dumps(summary, ensure_ascii=False) if summary is not None else None, now_iso(), sid),
            )

    def update_state(self, sid: str, state: dict) -> None:
        with self.conn() as c:
            c.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                      (json.dumps(state, ensure_ascii=False), now_iso(), sid))

    def update_content(self, sid: str, content: dict) -> None:
        """Only for sessions whose content is still being built (導讀 → 出題); records keep the final snapshot."""
        with self.conn() as c:
            c.execute("UPDATE sessions SET content_snapshot=?, updated_at=? WHERE id=?",
                      (json.dumps(content, ensure_ascii=False), now_iso(), sid))

    @staticmethod
    def _row_session(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("content_snapshot", "strategy_snapshot", "model_config", "state", "summary"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    # -- turns --------------------------------------------------------------
    def add_turn(self, sid: str, turn: dict[str, Any]) -> dict:
        with self.conn() as c:
            idx = c.execute("SELECT COALESCE(MAX(idx),0)+1 FROM turns WHERE session_id=?", (sid,)).fetchone()[0]
            cols = ["session_id", "idx", "created_at"] + list(turn.keys())
            vals = [sid, idx, now_iso()] + [
                json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in turn.values()
            ]
            c.execute(f"INSERT INTO turns ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            row = c.execute("SELECT * FROM turns WHERE session_id=? AND idx=?", (sid, idx)).fetchone()
        return self._row_turn(row)

    def list_turns(self, sid: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM turns WHERE session_id=? ORDER BY idx", (sid,)).fetchall()
        return [self._row_turn(r) for r in rows]

    @staticmethod
    def _row_turn(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("model_request", "judgement", "validation_notes", "decision", "tutor_message",
                  "state_before", "state_after", "usage"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, ValueError):
                    pass
        return d

    # -- settings -----------------------------------------------------------
    def get_setting(self, key: str) -> Any | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
