"""Single-worker runner for LangGraph sessions (plan §7.2–7.3).

A checkpoint says where a graph is; it does not schedule anything. The runner does:
- startup: take the single-runner lease (refuse to run beside another live owner), open the checkpointer, and
  re-schedule every queued / running command left by the previous process;
- per session one command at a time (DB claim is the authority, the asyncio task map only avoids double work);
- reconcile each command with the checkpoint before touching the graph:
    1. graph waits and `last_applied_event_id` is this command → it already ran: only mark it completed;
    2. graph is mid-run on this command (`pending_event_id` matches, no interrupt) → continue with invoke(None);
    3. graph waits for something else → deliver the event with Command(resume=…);
    4. a checkpoint behind a business commit is covered by 2/3: the re-run nodes find their committed operation
       keys and change nothing;
- after the graph stops: mark the command completed / failed and record where the graph waits, one transaction.
Model calls never run inside a SQLite write transaction."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .. import config
from . import faults
from . import invoke
from .migrations import upgrade_session
from .learning_graph import WorkflowDeps, build_learning_graph, initial_input, thread_config

log = logging.getLogger("mentor.runner")
LEASE_TTL = 30.0
HEARTBEAT = 10.0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class RunnerUnavailable(Exception):
    pass


class WorkflowRunner:
    def __init__(self, deps: WorkflowDeps, checkpoint_path: str, *, owner: str | None = None):
        self.deps = deps
        self.repo = deps.repo
        self.checkpoint_path = checkpoint_path
        self.owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.graph = None
        self.active = False
        self.refused_by: dict | None = None
        self._saver_cm = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._prefetch: dict[str, asyncio.Task] = {}
        self.prefetch_log: list[tuple[str, str]] = []   # (session, outcome), for tests / diagnostics
        self._beat: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        holder = self.repo.acquire_lease(self.owner, os.getpid(), time.time(), LEASE_TTL, pid_alive)
        if holder:
            self.refused_by = holder
            log.error("another workflow runner is active (pid %s); graph sessions are read-only here", holder["pid"])
            return
        self._saver_cm = AsyncSqliteSaver.from_conn_string(self.checkpoint_path)
        saver = await self._saver_cm.__aenter__()
        self.graph = build_learning_graph(self.deps, saver)
        self.active = True
        self._beat = asyncio.create_task(self._heartbeat())
        for cmd in self.repo.active_commands():
            self.notify(cmd["session_id"])

    async def stop(self) -> None:
        tasks = [t for t in list(self._tasks.values()) + list(self._prefetch.values()) if not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._beat:
            self._beat.cancel()
            await asyncio.gather(self._beat, return_exceptions=True)
        if self._saver_cm:
            await self._saver_cm.__aexit__(None, None, None)
        if self.active:
            self.repo.release_lease(self.owner)
        self.active = False

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT)
            if not self.repo.heartbeat(self.owner, time.time()):
                log.error("runner lease lost; stopping")
                self.active = False
                return

    async def idle(self, timeout: float = 30.0) -> None:
        """Wait until no session task is running (tests / shutdown)."""
        end = time.monotonic() + timeout
        while any(not t.done() for t in list(self._tasks.values()) + list(self._prefetch.values())):
            if time.monotonic() > end:
                raise TimeoutError("runner still busy")
            await asyncio.sleep(0.01)

    # -- scheduling ------------------------------------------------------------------------
    def notify(self, session_id: str) -> None:
        if not self.active:
            return
        p = self._prefetch.get(session_id)
        if p and not p.done():
            # a "next" needs exactly the guide being prefetched: let it finish and be adopted (cancelling it wastes the
            # calls already made — R8 measured every prefetch cancelled at a 20 s reading pace). Anything else
            # (a question, finish, an answer) changes what the next guide depends on: interactive work first.
            pending = self.repo.active_commands(session_id)
            if not (pending and all(c["type"] == "next" for c in pending)):
                p.cancel()   # an inference already running in the model is not pre-empted
        t = self._tasks.get(session_id)
        if t is None or t.done():
            self._tasks[session_id] = asyncio.create_task(self._drain(session_id))

    async def _drain(self, session_id: str) -> None:
        while self.active:
            pending = self.repo.active_commands(session_id)
            if not pending:
                break
            p = self._prefetch.get(session_id)
            if p and not p.done():
                await asyncio.wait({p})   # a kept prefetch (see notify) finishes first; its guide is then adopted
            await self._execute(pending[0])
        if self.active and config.READING_PREFETCH:
            row = self.deps.db.get_session(session_id)
            if row and (row.get("workflow_status") or {}).get("waiting") == "student":
                self._prefetch[session_id] = asyncio.create_task(self._run_prefetch(session_id))

    async def _run_prefetch(self, session_id: str) -> None:
        upgrade_session(self.deps.db, session_id)
        try:
            outcome = await self.graph.prefetch_next(session_id)
        except asyncio.CancelledError:
            self.prefetch_log.append((session_id, "cancelled"))
            raise
        except Exception as e:  # noqa: BLE001 — a failed prefetch only means the guide is written on demand
            outcome = f"error: {type(e).__name__}: {e}"
        self.prefetch_log.append((session_id, outcome))

    async def _execute(self, cmd: dict) -> None:
        sid, cid = cmd["session_id"], cmd["command_id"]
        claimed = self.repo.claim_command(sid, cid, self.owner)
        if claimed is None:
            return
        try:
            await self._run(claimed)
        except asyncio.CancelledError:
            raise   # shutdown: the command stays `running` and is recovered by the next start
        except Exception as e:  # noqa: BLE001 — a command must never stay running forever because of a bug
            log.exception("command %s failed", cid)
            self.repo.finish_command(sid, cid, status="failed", retryable=False,
                                     error={"code": "internal", "detail": f"{type(e).__name__}: {e}"})

    # -- reconcile + run ----------------------------------------------------------------------
    @staticmethod
    def _waiting(snap) -> dict | None:
        for t in snap.tasks:
            for i in t.interrupts:
                return i.value
        return None

    def event_of(self, cmd: dict) -> dict:
        return {"type": cmd["type"], "command_id": cmd["command_id"], "payload": cmd["payload"] or {}}

    async def _restart_stale_guides(self, cfg: dict) -> None:
        """A guide run interrupted under other gate rules (its checkpoint carries an older gates.GATE_RULES_VERSION,
        or none) must not continue: LangGraph would resume the subgraph at the interrupted node with its old state,
        skipping the source verification the new rules require. Writing to the parent state makes a new checkpoint,
        so the pending guide node gets a new task id and its subgraph starts from the beginning (new operation key,
        new rules). What the discarded run spent is carried over; the budget is never refilled."""
        from ..llm.runtime import CallBudget
        from ..reading import gates
        snap = await self.graph.aget_state(cfg, subgraphs=True)
        carry = {}
        for t in snap.tasks:
            sv = (t.state.values if t.state is not None else None) or {}
            if "task" not in sv or "op" not in sv or sv.get("gate_rules") == gates.GATE_RULES_VERSION:
                continue
            b = CallBudget.from_dict(sv["budget"])
            # same reconciliation as the guide itself: carried base + every call traced under that run's key
            b.calls = max(b.calls, int(sv.get("budget_base") or 0) + self.repo.count_calls(sv["op"] + ":"))
            carry[f"{sv['task']['unit_id']}:a{sv['task'].get('attempt', 1)}"] = b.to_dict()
            log.info("guide %s ran under gate rules %s, now %s: restarting it", sv["op"], sv.get("gate_rules"),
                     gates.GATE_RULES_VERSION)
        if carry:
            await self.graph.aupdate_state(cfg, {"guide_budget_carry": {**(snap.values.get("guide_budget_carry") or {}),
                                                                        **carry}})

    async def _run(self, cmd: dict) -> None:
        sid, cid = cmd["session_id"], cmd["command_id"]
        upgrade_session(self.deps.db, sid)   # R7: a session from an older reading workflow is upgraded once, recorded
        cfg = thread_config(sid)
        snap = await self.graph.aget_state(cfg)
        v = snap.values or {}
        if not v:
            await invoke(self.graph, initial_input(sid, cid), cfg)
        elif v.get("last_applied_event_id") == cid and (self._waiting(snap) or not snap.next):
            pass                                                     # case 1: ran before the restart
        elif v.get("pending_event_id") == cid and snap.next and not self._waiting(snap):
            await self._restart_stale_guides(cfg)
            await invoke(self.graph, None, cfg)                      # case 2: continue the interrupted run
        elif self._waiting(snap):
            await invoke(self.graph, Command(resume=self.event_of(cmd)), cfg)   # case 3: deliver
        elif snap.next:
            await self._restart_stale_guides(cfg)
            await invoke(self.graph, None, cfg)                      # another event's run was cut short
            return await self._run(cmd)
        else:
            self.repo.finish_command(sid, cid, status="failed", error={"code": "completed", "detail": "本次學習已完成"})
            return
        snap = await self.graph.aget_state(cfg)
        v = snap.values
        faults.hit("before_complete")
        waiting = self._waiting(snap)
        if v.get("last_applied_event_id") != cid:
            raise RuntimeError(f"graph stopped without applying {cid} (next={snap.next})")
        out = v.get("outcome") or {"status": "completed"}
        err = out.get("error")
        ws = {"waiting": (waiting or {}).get("waiting") if waiting else ("completed" if not snap.next else None),
              "failed_node": v.get("failed_node") if v.get("error") else None,
              "error": v.get("error"), "failed_command_id": cid if v.get("error") else None,
              "last_command_id": cid, "last_failed": cid if err else None}
        self.repo.finish_command(sid, cid, status=out["status"], result=out.get("result"), error=err,
                                 retryable=bool(err and err.get("retryable")), workflow_status=ws)
