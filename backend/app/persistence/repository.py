"""Business-data repository for the LangGraph workflow (the SQLite business DB is the authority; checkpoints
only hold where a graph is). Grows with the phases: source snapshots (P2), artifacts + llm_calls (P3), commands
and atomic commits (P4)."""
from __future__ import annotations

import json
import uuid

from ..db import Database, now_iso
from ..llm.runtime import CallRecord
from ..reading.schemas import Locator, SourceBlock


def _j(v):
    return json.dumps(v, ensure_ascii=False, default=str) if v is not None else None


class DbSink:
    """TraceSink writing each CallRecord as soon as the call returns (before any graph checkpoint)."""

    def __init__(self, repo: "Repository"):
        self.repo = repo

    def record(self, rec: CallRecord) -> None:
        self.repo.add_llm_call(rec)


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # -- source snapshots -----------------------------------------------------------
    def save_snapshot(self, material_id: str, snap: dict, blocks: list[SourceBlock]) -> None:
        """Content-addressed: the same range extracted the same way is stored once."""
        with self.db.conn() as c:
            if c.execute("SELECT 1 FROM source_snapshots WHERE snapshot_id=?", (snap["snapshot_id"],)).fetchone():
                return
            c.execute('INSERT INTO source_snapshots (snapshot_id, material_id, unit, start, "end", extraction_version, '
                      "text_hash, block_count, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (snap["snapshot_id"], material_id, snap["unit"], snap["start"], snap["end"],
                       snap["extraction_version"], snap["text_hash"], len(blocks), now_iso()))
            c.executemany("INSERT INTO source_blocks (block_id, snapshot_id, ordinal, kind, text, locator, text_hash, "
                          "extraction_version, parent_block_id, piece, continues, heading_level, structure_confidence, meta) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [(b.block_id, b.material_snapshot_id, b.ordinal, b.kind, b.text, b.locator.model_dump_json(),
                            b.text_hash, b.extraction_version, b.parent_block_id, b.piece, int(b.continues),
                            b.heading_level, b.structure_confidence, _j(b.meta) if b.meta else None) for b in blocks])

    def load_blocks(self, snapshot_id: str) -> list[SourceBlock]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM source_blocks WHERE snapshot_id=? ORDER BY ordinal", (snapshot_id,)).fetchall()
        return [SourceBlock(block_id=r["block_id"], material_snapshot_id=r["snapshot_id"], ordinal=r["ordinal"],
                            kind=r["kind"], text=r["text"], locator=Locator.model_validate(json.loads(r["locator"])),
                            text_hash=r["text_hash"], extraction_version=r["extraction_version"],
                            parent_block_id=r["parent_block_id"], piece=r["piece"], continues=bool(r["continues"]),
                            heading_level=r["heading_level"], structure_confidence=r["structure_confidence"],
                            meta=json.loads(r["meta"]) if r["meta"] else {})
                for r in rows]

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM source_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        return dict(r) if r else None

    # -- llm call trace -------------------------------------------------------------
    def add_llm_call(self, r: CallRecord) -> None:
        with self.db.conn() as c:
            c.execute("INSERT INTO llm_calls (session_id, command_id, operation_key, task, node, attempt, purpose, "
                      "prompt_version, request, raw, validated, source_ids, usage, latency_ms, error, error_kind, backend, "
                      "model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r.session_id, r.command_id, r.operation_key, r.task, r.node, r.attempt, r.purpose, r.prompt_version,
                       _j(r.request), r.raw, _j(r.validated), _j(r.source_ids), _j(r.usage), r.latency_ms, r.error,
                       r.error_kind, r.backend, r.model, now_iso()))

    def list_llm_calls(self, session_id: str | None = None, *, operation_prefix: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM llm_calls WHERE 1=1", []
        if session_id is not None:
            q += " AND session_id=?"
            args.append(session_id)
        if operation_prefix is not None:
            q += " AND operation_key LIKE ?"
            args.append(operation_prefix + "%")
        with self.db.conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("request", "validated", "source_ids", "usage"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out

    def count_calls(self, operation_prefix: str) -> int:
        with self.db.conn() as c:
            return c.execute("SELECT COUNT(*) FROM llm_calls WHERE operation_key LIKE ?", (operation_prefix + "%",)).fetchone()[0]

    # -- artifacts -------------------------------------------------------------------
    def get_artifact(self, operation_key: str) -> dict | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM reading_artifacts WHERE operation_key=?", (operation_key,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def put_artifact(self, operation_key: str, *, kind: str, status: str, payload, session_id: str | None = None,
                     unit_id: str | None = None, source_hash: str | None = None, context_hash: str | None = None,
                     prompt_version: str | None = None, model_config_hash: str | None = None,
                     workflow_version: str | None = None, conn=None) -> dict:
        """Insert once per operation key; an existing row wins (idempotent re-run after a restart)."""
        def run(c):
            if not c.execute("SELECT 1 FROM reading_artifacts WHERE operation_key=?", (operation_key,)).fetchone():
                c.execute("INSERT INTO reading_artifacts (artifact_id, session_id, kind, unit_id, operation_key, status, payload, "
                          "source_hash, context_hash, prompt_version, model_config_hash, workflow_version, created_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          ("art-" + uuid.uuid4().hex[:12], session_id, kind, unit_id, operation_key, status, _j(payload),
                           source_hash, context_hash, prompt_version, model_config_hash, workflow_version, now_iso()))
        if conn is not None:
            run(conn)
        else:
            with self.db.conn() as c:
                run(c)
        return self.get_artifact(operation_key) if conn is None else {"operation_key": operation_key}

    def list_artifacts(self, session_id: str, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM reading_artifacts WHERE session_id=?", [session_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        with self.db.conn() as c:
            rows = c.execute(q + " ORDER BY created_at, rowid", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    # -- workflow commands --------------------------------------------------------------
    @staticmethod
    def _row_cmd(r) -> dict:
        d = dict(r)
        for k in ("payload", "result", "error"):
            d[k] = json.loads(d[k]) if d[k] else None
        d["retryable"] = bool(d["retryable"])
        return d

    def get_command(self, session_id: str, command_id: str) -> dict | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM workflow_commands WHERE session_id=? AND command_id=?", (session_id, command_id)).fetchone()
        return self._row_cmd(r) if r else None

    def insert_command(self, session_id: str, command_id: str, type_: str, payload: dict, payload_hash: str,
                       expected_revision: int | None, *, client_supplied: bool = True) -> tuple[dict, bool]:
        """(command, created). Idempotent on (session, command_id): an existing row is returned as is."""
        ts = now_iso()
        with self.db.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM workflow_commands WHERE session_id=? AND command_id=?", (session_id, command_id)).fetchone()
            if r:
                return self._row_cmd(r), False
            c.execute("INSERT INTO workflow_commands (command_id, session_id, type, payload, payload_hash, expected_revision, "
                      "status, client_supplied, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (command_id, session_id, type_, _j(payload), payload_hash, expected_revision, "queued",
                       int(client_supplied), ts, ts))
            r = c.execute("SELECT * FROM workflow_commands WHERE session_id=? AND command_id=?", (session_id, command_id)).fetchone()
        return self._row_cmd(r), True

    def active_commands(self, session_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM workflow_commands WHERE status IN ('queued','running')"
        args: list = []
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        with self.db.conn() as c:
            return [self._row_cmd(r) for r in c.execute(q + " ORDER BY seq", args)]

    def claim_command(self, session_id: str, command_id: str, owner: str) -> dict | None:
        """queued → running (or re-claim a running row of a dead owner). Returns the row, None if not claimable."""
        with self.db.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            n = c.execute("UPDATE workflow_commands SET status='running', claim_owner=?, claimed_at=?, attempt=attempt+1, "
                          "updated_at=? WHERE session_id=? AND command_id=? AND status IN ('queued','running')",
                          (owner, now_iso(), now_iso(), session_id, command_id)).rowcount
            r = c.execute("SELECT * FROM workflow_commands WHERE session_id=? AND command_id=?", (session_id, command_id)).fetchone()
        return self._row_cmd(r) if n else None

    def set_command_stage(self, session_id: str, command_id: str | None, stage: str) -> None:
        if not command_id:
            return
        with self.db.conn() as c:
            c.execute("UPDATE workflow_commands SET stage=?, updated_at=? WHERE session_id=? AND command_id=? AND status='running'",
                      (stage, now_iso(), session_id, command_id))

    def finish_command(self, session_id: str, command_id: str, *, status: str, result=None, error=None,
                       retryable: bool = False, workflow_status: dict | None = None) -> None:
        """Mark the command done and record where the graph now waits — one transaction."""
        with self.db.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE workflow_commands SET status=?, result=?, error=?, retryable=?, updated_at=? "
                      "WHERE session_id=? AND command_id=?",
                      (status, _j(result), _j(error), int(retryable), now_iso(), session_id, command_id))
            if workflow_status is not None:
                c.execute("UPDATE sessions SET workflow_status=? WHERE id=?", (_j(workflow_status), session_id))

    def list_commands(self, session_id: str) -> list[dict]:
        with self.db.conn() as c:
            return [self._row_cmd(r) for r in c.execute("SELECT * FROM workflow_commands WHERE session_id=? ORDER BY seq", (session_id,))]

    # -- atomic business commits ------------------------------------------------------------
    def commit(self, session_id: str, operation_key: str, *, kind: str, payload, mutate=None, unit_id: str | None = None,
               status: str | None = None, summary=None, turn: dict | None = None, artifact_status: str = "accepted",
               expected_revision: int | None = None, meta: dict | None = None) -> tuple[dict, bool]:
        """Apply one business step exactly once: the artifact row (unique operation key), the session's content /
        state change (`mutate(content, state)`), an optional turn, revision + 1 — all in one transaction.
        Returns (artifact payload, applied_now). A repeated key returns the stored payload and changes nothing."""
        with self.db.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT payload FROM reading_artifacts WHERE operation_key=?", (operation_key,)).fetchone()
            if r:
                return json.loads(r["payload"]), False
            row = c.execute("SELECT content_snapshot, state, revision, status, summary FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            if expected_revision is not None and row["revision"] != expected_revision:
                raise StaleRevision(session_id, expected_revision, row["revision"])
            content, state = json.loads(row["content_snapshot"]), json.loads(row["state"])
            if mutate:
                mutate(content, state)
            new_status = status or row["status"]
            c.execute("UPDATE sessions SET content_snapshot=?, state=?, revision=revision+1, status=?, summary=?, updated_at=? WHERE id=?",
                      (_j(content), _j(state), new_status, _j(summary) if summary is not None else row["summary"], now_iso(), session_id))
            m = meta or {}
            c.execute("INSERT INTO reading_artifacts (artifact_id, session_id, kind, unit_id, operation_key, status, payload, "
                      "source_hash, context_hash, prompt_version, model_config_hash, workflow_version, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("art-" + uuid.uuid4().hex[:12], session_id, kind, unit_id, operation_key, artifact_status, _j(payload),
                       m.get("source_hash"), m.get("context_hash"), m.get("prompt_version"), m.get("model_config_hash"),
                       m.get("workflow_version"), now_iso()))
            if turn is not None:
                idx = c.execute("SELECT COALESCE(MAX(idx),0)+1 FROM turns WHERE session_id=?", (session_id,)).fetchone()[0]
                cols = ["session_id", "idx", "created_at", "operation_key"] + list(turn.keys())
                vals = [session_id, idx, now_iso(), operation_key] + [_j(v) if isinstance(v, (dict, list)) else v for v in turn.values()]
                c.execute(f"INSERT INTO turns ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return payload, True

    def set_workflow_status(self, session_id: str, status: dict) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE sessions SET workflow_status=? WHERE id=?", (_j(status), session_id))

    def session_revision(self, session_id: str) -> int:
        with self.db.conn() as c:
            return c.execute("SELECT revision FROM sessions WHERE id=?", (session_id,)).fetchone()[0]

    def graph_sessions(self) -> list[str]:
        with self.db.conn() as c:
            return [r[0] for r in c.execute("SELECT id FROM sessions WHERE workflow_runner='langgraph'")]

    # -- runner lease -----------------------------------------------------------------------
    def acquire_lease(self, owner: str, pid: int, now: float, ttl: float, alive) -> dict | None:
        """Take the single-runner lease. Returns the other live holder (refused) or None (acquired)."""
        with self.db.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT owner, pid, heartbeat_at FROM runner_lease WHERE id=1").fetchone()
            if r and r["owner"] != owner and now - r["heartbeat_at"] < ttl and alive(r["pid"]):
                return dict(r)
            c.execute("INSERT INTO runner_lease (id, owner, pid, heartbeat_at) VALUES (1,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, pid=excluded.pid, heartbeat_at=excluded.heartbeat_at",
                      (owner, pid, now))
        return None

    def heartbeat(self, owner: str, now: float) -> bool:
        with self.db.conn() as c:
            return c.execute("UPDATE runner_lease SET heartbeat_at=? WHERE id=1 AND owner=?", (now, owner)).rowcount == 1

    def release_lease(self, owner: str) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM runner_lease WHERE id=1 AND owner=?", (owner,))


class StaleRevision(Exception):
    def __init__(self, session_id: str, expected: int, actual: int):
        super().__init__(f"{session_id}: expected revision {expected}, now {actual}")
        self.expected, self.actual = expected, actual
