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


# Ordered migrations on top of SCHEMA (the original tables = version 1). Each runs once, in one transaction,
# and only adds tables / nullable columns, so rows written by older code stay readable. Never edit a shipped one.
MIGRATIONS: list[tuple[int, str, str]] = [
    (2, "workflow session metadata + source snapshots", """
ALTER TABLE sessions ADD COLUMN workflow_runner TEXT;          -- NULL = legacy (rows from before this migration)
ALTER TABLE sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN workflow_meta TEXT;           -- immutable: workflow/schema version, prompt bundle, model snapshot
CREATE TABLE IF NOT EXISTS source_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  material_id TEXT NOT NULL,
  unit TEXT NOT NULL,
  start INTEGER NOT NULL,
  "end" INTEGER NOT NULL,
  extraction_version TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  block_count INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_blocks (
  block_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
  ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  locator TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  parent_block_id TEXT,
  piece INTEGER,
  continues INTEGER NOT NULL DEFAULT 0,
  UNIQUE (snapshot_id, ordinal)
);
"""),
    (3, "reading artifacts + llm call trace", """
CREATE TABLE IF NOT EXISTS reading_artifacts (
  artifact_id TEXT PRIMARY KEY,
  session_id TEXT,
  kind TEXT NOT NULL,                 -- guide | answer | compact | quiz | assess | step | segment_window
  unit_id TEXT,
  operation_key TEXT NOT NULL UNIQUE, -- session + command + node + logical item + version
  status TEXT NOT NULL,               -- draft | accepted | rejected | cached
  payload TEXT NOT NULL,
  source_hash TEXT,
  context_hash TEXT,
  prompt_version TEXT,
  model_config_hash TEXT,
  workflow_version TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reading_artifacts_session ON reading_artifacts(session_id, kind);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  command_id TEXT,
  operation_key TEXT,
  task TEXT NOT NULL,
  node TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  purpose TEXT NOT NULL,
  prompt_version TEXT,
  request TEXT,
  raw TEXT,
  validated TEXT,
  source_ids TEXT,
  usage TEXT,
  latency_ms INTEGER,
  error TEXT,
  error_kind TEXT,
  backend TEXT,
  model TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_calls_session ON llm_calls(session_id, id);
CREATE INDEX IF NOT EXISTS llm_calls_op ON llm_calls(operation_key);
"""),
    (4, "durable workflow commands + runner lease", """
CREATE TABLE IF NOT EXISTS workflow_commands (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  type TEXT NOT NULL,                 -- start | guide | ask | next | finish | answer | retry
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  expected_revision INTEGER,
  status TEXT NOT NULL,               -- queued | running | completed | failed
  stage TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  result TEXT,
  error TEXT,
  retryable INTEGER NOT NULL DEFAULT 0,
  client_supplied INTEGER NOT NULL DEFAULT 1,
  claim_owner TEXT,
  claimed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (session_id, command_id)
);
CREATE INDEX IF NOT EXISTS workflow_commands_status ON workflow_commands(status, seq);
CREATE TABLE IF NOT EXISTS runner_lease (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  owner TEXT NOT NULL,
  pid INTEGER NOT NULL,
  heartbeat_at REAL NOT NULL
);
ALTER TABLE turns ADD COLUMN command_id TEXT;
ALTER TABLE turns ADD COLUMN operation_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS turns_operation ON turns(operation_key) WHERE operation_key IS NOT NULL;
ALTER TABLE sessions ADD COLUMN workflow_status TEXT;     -- where the graph waits: {waiting, stage, error, …}
"""),
]

SESSION_COLUMNS = ("id", "created_at", "updated_at", "status", "content_snapshot", "strategy_snapshot", "model_config",
                   "state", "summary", "workflow_runner", "revision", "workflow_meta")


def migrate(c: sqlite3.Connection, migrations: list[tuple[int, str, str]] | None = None) -> list[int]:
    """Apply pending migrations in order; returns the versions applied now."""
    c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    if c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0:
        c.execute("INSERT INTO schema_migrations VALUES (1, 'initial schema', ?)", (now_iso(),))
    done = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, sql in sorted(migrations if migrations is not None else MIGRATIONS):
        if version in done:
            continue
        c.execute("BEGIN")
        try:
            for stmt in (x.strip() for x in sql.split(";")):
                if stmt and not all(ln.strip().startswith("--") for ln in stmt.splitlines() if ln.strip()):
                    c.execute(stmt)
            c.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (version, name, now_iso()))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


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
        self._migrate()

    def _migrate(self) -> None:
        if self._mem is not None:
            c = self._mem
            c.isolation_level = None
            migrate(c)
            c.isolation_level = ""
            return
        c = sqlite3.connect(self.path, isolation_level=None)
        try:
            migrate(c)
        finally:
            c.close()

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
    def create_session(self, content: dict, strategy: dict, model_config: dict, state: dict, *,
                       workflow_runner: str | None = None, workflow_meta: dict | None = None,
                       sid: str | None = None) -> dict:
        sid, ts = sid or new_id(), now_iso()
        cols = ("id", "created_at", "updated_at", "status", "content_snapshot", "strategy_snapshot", "model_config",
                "state", "workflow_runner", "revision", "workflow_meta")
        with self.conn() as c:
            c.execute(
                f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                (sid, ts, ts, "active", json.dumps(content, ensure_ascii=False),
                 json.dumps(strategy, ensure_ascii=False), json.dumps(model_config, ensure_ascii=False),
                 json.dumps(state, ensure_ascii=False), workflow_runner, 0,
                 json.dumps(workflow_meta, ensure_ascii=False) if workflow_meta is not None else None),
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
        for k in ("content_snapshot", "strategy_snapshot", "model_config", "state", "summary", "workflow_meta", "workflow_status"):
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
