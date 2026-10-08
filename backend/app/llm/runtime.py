"""Model runtime for the LangGraph workflow: immutable model snapshot, shared call budget, call trace, and one
inference at a time per physical model.

- ModelSnapshot is taken when a session starts and stored with it; later setting changes never reach that session
  (no reads of the mutable Registry inside an await).
- CallBudget is owned by one task (a window plan, one guide, one answer …). Every model call — first try, format
  fix, semantic revision — is charged here and nowhere else; the bridge and LangChain add no retries of their own.
  Its counters are plain data so they can live in graph state / be rebuilt from llm_calls after a restart.
- CallRecord is one row of the trace (llm_calls table): why the call happened, which sources it saw, how long it
  took, what came back and whether it validated.
- BackendPool hands out one adapter + one asyncio.Semaphore per physical model (backend, model, host, path,
  device), so an OpenVINO pipeline is loaded once and inferences never overlap."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ..models import ModelConfig
from .base import LLMBackend
from .langchain_bridge import BackendChatModel


def stable_hash(obj: Any, n: int = 16) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:n]


@dataclass(frozen=True)
class ModelSnapshot:
    backend: str
    model: str
    host: str
    model_path: str
    device: str
    temperature: float
    max_tokens: int
    context_tokens: int

    @classmethod
    def from_config(cls, cfg: ModelConfig | dict) -> "ModelSnapshot":
        d = cfg.model_dump() if isinstance(cfg, ModelConfig) else cfg
        return cls(**{k: d.get(k, ModelConfig.model_fields[k].default) for k in cls.__dataclass_fields__})

    def to_config(self) -> ModelConfig:
        return ModelConfig(**asdict(self))

    @property
    def identity(self) -> tuple:
        """What makes two snapshots the same physical model (generation params excluded)."""
        if self.backend == "ollama":
            return ("ollama", self.host, self.model)
        if self.backend == "remote":     # one remote endpoint; its URL may change (Colab restart), not its identity
            return ("remote", self.model)
        if self.backend == "openvino":
            return ("openvino", self.model_path, self.device)
        return ("mock",)

    @property
    def config_hash(self) -> str:
        return stable_hash(asdict(self))


# -- budget ---------------------------------------------------------------------

class BudgetExceeded(Exception):
    def __init__(self, task: str, what: str):
        super().__init__(f"{task}: {what} budget exhausted")
        self.task, self.what = task, what


# Initial engineering limits from the plan (§5.5); calls include format fixes and revisions.
BUDGETS: dict[str, dict[str, int]] = {
    "segment_window": {"max_calls": 2, "max_format_fixes": 1, "max_revisions": 0},
    "segment_split": {"max_calls": 2, "max_format_fixes": 1, "max_revisions": 0},   # one oversize unit, focused
    "segment_topic": {"max_calls": 4, "max_format_fixes": 2, "max_revisions": 0},   # one unit: summaries, then where topics change
    "segment_outline": {"max_calls": 2, "max_format_fixes": 1, "max_revisions": 0},  # outlines of one batch of 區塊
    "segment_matter": {"max_calls": 2, "max_format_fixes": 1, "max_revisions": 0},   # which 區塊 of a stretch need no guide
    "segment_boundaries": {"max_calls": 2, "max_format_fixes": 1, "max_revisions": 0},  # same unit / new unit, pair by pair
    "topic": {"max_calls": 2, "max_format_fixes": 1, "max_revisions": 0},            # one unit's summary, key points, check
    # one 區塊's handout: symbols, draft, the two reviews, then up to three corrections each re-checked by both
    # reviews (13 calls); format fixes are calls too
    "guide": {"max_calls": 17, "max_format_fixes": 4, "max_revisions": 3, "max_tool_rounds": 0, "max_tools_per_round": 0},
    "ask": {"max_calls": 4, "max_format_fixes": 1, "max_revisions": 1, "max_tool_rounds": 1, "max_tools_per_round": 2},
    "compact": {"max_calls": 5, "max_format_fixes": 1, "max_revisions": 1},     # notes, audit, one revision + audit
    "generate": {"max_calls": 3, "max_format_fixes": 1, "max_revisions": 0},    # questions, audit
    "assess": {"max_calls": 2, "max_format_fixes": 1, "max_revisions": 0},
}


@dataclass
class CallBudget:
    task: str
    max_calls: int
    max_format_fixes: int = 1
    max_revisions: int = 0
    max_tool_rounds: int = 0
    max_tools_per_round: int = 0
    calls: int = 0
    format_fixes: int = 0
    revisions: int = 0
    tool_rounds: int = 0
    tool_calls: int = 0

    @classmethod
    def for_task(cls, task: str, **used: int) -> "CallBudget":
        return cls(task=task, **BUDGETS[task], **used)

    def charge_call(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(self.task, "call")
        self.calls += 1

    def can(self, what: str) -> bool:
        if self.calls >= self.max_calls:
            return False
        if what == "format_fix":
            return self.format_fixes < self.max_format_fixes
        if what == "revision":
            return self.revisions < self.max_revisions
        if what == "tool_round":
            return self.tool_rounds < self.max_tool_rounds
        return True

    def charge(self, what: str) -> None:
        if not self.can(what):
            raise BudgetExceeded(self.task, what)
        if what == "format_fix":
            self.format_fixes += 1
        elif what == "revision":
            self.revisions += 1
        elif what == "tool_round":
            self.tool_rounds += 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CallBudget":
        return cls(**d)


# -- trace ---------------------------------------------------------------------

@dataclass
class CallRecord:
    task: str                 # guide | ask | segment_window | …
    node: str                 # graph node / step that made the call
    attempt: int              # 1-based within the task (format fix / revision are later attempts)
    purpose: str              # first | format_fix | revision | context | review …
    prompt_version: str
    request: dict             # messages + generation params (no schema dump)
    source_ids: list[str] = field(default_factory=list)
    raw: str | None = None
    validated: Any = None
    error: str | None = None
    error_kind: str | None = None   # unavailable | invalid_output | budget
    usage: dict = field(default_factory=dict)
    latency_ms: int | None = None
    backend: str = ""
    model: str = ""
    session_id: str | None = None
    command_id: str | None = None
    operation_key: str | None = None

    def as_row(self) -> dict:
        return asdict(self)


class TraceSink(Protocol):
    def record(self, rec: CallRecord) -> None: ...


class ListSink:
    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    def record(self, rec: CallRecord) -> None:
        self.records.append(rec)


# -- backend pool ------------------------------------------------------------------

class BackendPool:
    """One adapter and one inference semaphore per physical model. `seed` lets the app hand over the Registry's
    already-built adapter (an OpenVINO pipeline must not be loaded twice)."""

    def __init__(self, build=None, parallel: int = 1):
        from .registry import build_backend
        self._build = build or build_backend
        self._backends: dict[tuple, LLMBackend] = {}
        self._locks: dict[tuple, asyncio.Semaphore] = {}
        self.parallel = max(1, parallel)

    def set_parallel(self, n: int) -> None:
        """Requests one physical model may serve at once (the settings' 並行數量). Semaphores are made anew; calls
        already holding an old one finish under it."""
        n = max(1, int(n))
        if n != self.parallel:
            self.parallel = n
            self._locks.clear()

    def seed(self, snap: ModelSnapshot, backend: LLMBackend) -> None:
        self._backends.setdefault(snap.identity, backend)

    def backend(self, snap: ModelSnapshot) -> LLMBackend:
        if snap.identity not in self._backends:
            self._backends[snap.identity] = self._build(snap.to_config())
        return self._backends[snap.identity]

    def semaphore(self, snap: ModelSnapshot) -> asyncio.Semaphore:
        return self._locks.setdefault(snap.identity, asyncio.Semaphore(self.parallel))


class ModelRuntime:
    """What a task needs to call the model of one session: the snapshot, the adapter and its semaphore."""

    def __init__(self, snap: ModelSnapshot, pool: BackendPool):
        self.snap = snap
        self.backend = pool.backend(snap)
        self.semaphore = pool.semaphore(snap)

    def chat_model(self, *, min_max_tokens: int = 0, min_context_tokens: int = 0) -> BackendChatModel:
        """Task minimums only raise the session's limits (same rule as the legacy agent's max(cfg, task))."""
        return BackendChatModel(backend=self.backend, temperature=self.snap.temperature,
                                max_tokens=max(self.snap.max_tokens, min_max_tokens),
                                context_tokens=max(self.snap.context_tokens, min_context_tokens),
                                semaphore=self.semaphore)

    def max_tokens(self, min_max_tokens: int = 0) -> int:
        return max(self.snap.max_tokens, min_max_tokens)

    async def limits(self, task_floor: int = 0):
        """Effective context limits of this model (capabilities of the loaded backend, capped by config)."""
        from .context import resolve_limits
        return resolve_limits(await self.backend.capabilities(), self.snap.context_tokens, task_floor)
