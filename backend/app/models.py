"""Pydantic schemas shared by the API and the tutor engine."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Assessment = Literal["correct", "partial", "incorrect", "unclear"]
Action = Literal["advance", "probe", "hint", "explain", "pass"]


class ModelJudgement(BaseModel):
    """Structured result the model must return. Validated before the rules run."""

    assessment: Assessment
    reasoning_ok: bool = False
    evidence: str = ""
    feedback: str
    hint: str | None = None
    probe: str | None = None
    source_ids: list[str] = Field(default_factory=list)


class ModelConfig(BaseModel):
    backend: Literal["mock", "ollama", "remote", "openvino"] = "mock"
    model: str = ""
    host: str = ""          # ollama host (local / LAN); "remote" uses the endpoint saved in the settings instead
    model_path: str = ""    # openvino model dir
    device: str = "GPU"     # openvino device
    temperature: float = 0.2
    max_tokens: int = 512
    context_tokens: int = 8192
    # requests the model server may run at once (Ollama: OLLAMA_NUM_PARALLEL; the Colab notebook prints it). Above 1
    # the reading workflow verifies the sources of the next units while the reader reads. Not part of the session's
    # model snapshot: it changes speed, not output.
    parallel: int = Field(default=1, ge=1, le=8)


class RemoteEndpointRequest(BaseModel):
    """The remote model server (Colab or any server behind the same token proxy). An empty token keeps the saved one."""

    url: str = Field(max_length=300)
    token: str | None = Field(default=None, max_length=300)


class CompanionSettings(BaseModel):
    """伴讀角色：每個角色背後各自綁一組模型設定；active 的那組就是目前的模型設定。"""

    active: str
    companions: dict[str, ModelConfig]


class StartSessionRequest(BaseModel):
    """Either a built-in content file, or a range of an imported material (units are 1-based, inclusive)."""

    content_id: str = "correlation-causation"
    strategy_id: str = "rules-v1"
    material_id: str | None = None
    start: int | None = None
    end: int | None = None
    # task contract of a LangGraph reading session (optional; defaults are recorded as defaults)
    reading_depth: Literal["close", "skim"] | None = None
    reading_goal: str | None = Field(default=None, max_length=200)
    reader_background: str | None = Field(default=None, max_length=500)


class ReaderAsk(BaseModel):
    """A question typed in the reader: `at` is the page (PDF) or line (md / txt) being read."""

    at: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=4000)


class ReaderLearn(BaseModel):
    at: int = Field(ge=1)
    strategy_id: str = "rules-v1"


class TourOpen(BaseModel):
    """導讀 pressed in the reader at page (line) `at`; `span`: the pages taken on either side (設定 › 導讀)."""

    at: int = Field(ge=1)
    span: int = Field(default=2, ge=0, le=10)


class TourFocus(BaseModel):
    """The 主題 on screen in the 導讀 mode (`index` in window `session_id`), or a window picked in the sidebar."""

    session_id: str
    index: int | None = Field(default=None, ge=0)
    at: int | None = Field(default=None, ge=1)


class TourExtend(BaseModel):
    session_id: str
    direction: Literal["before", "after"]
    span: int = Field(default=2, ge=0, le=10)


class TourRetry(BaseModel):
    session_id: str
    index: int | None = Field(default=None, ge=0)


class CategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class MaterialUpdate(BaseModel):
    title: str | None = None
    category_id: str | None = None
    translate_zh: bool | None = None   # guides of a non-Chinese material in 繁體中文 (default: in the source language)


class BookmarkRequest(BaseModel):
    chapter: str | None = Field(None, max_length=300)


class LabelRequest(BaseModel):
    label: str | None = Field(None, max_length=80)


class CategoryOrder(BaseModel):
    ids: list[str] = Field(max_length=200)


class ShelfQuery(BaseModel):
    text: str = Field(default="", max_length=200)


class ShelfRoot(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    enabled: bool = True


class ShelfScope(BaseModel):
    roots: list[ShelfRoot] = Field(max_length=30)
    exclude: list[str] = Field(default_factory=list, max_length=50)


class ShelfImport(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=20)
    category_name: str | None = Field(default=None, max_length=40)
    # an existing reading area to put the books in (wins over category_name, which makes or finds one by name)
    category_id: str | None = Field(default=None, max_length=64)


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1)


class AskRequest(BaseModel):
    question: str = Field(min_length=1)


class ReadingView(BaseModel):
    """導讀 progress: quotes of the range, which one is current, and everything generated so far."""

    index: int
    total: int
    done: bool
    quotes: list[dict[str, Any]]
    parts: list[dict[str, Any]]
    guides: dict[str, dict[str, Any]]
    qa: dict[str, list[dict[str, Any]]]
    compacts: dict[str, dict[str, Any]]
    quiz_source: str | None = None   # "full" | "compact" once the questions have been written
    segmentation: dict[str, Any] | None = None   # how the quotes were decided: planner, candidate count, fallback notes, status
    stopped_at: int | None = None    # set when the student went to the questions before the last quote
    job: dict[str, Any] | None = None   # background work in progress ({"stage", "error"}); poll the session until it is None


class TutorMessage(BaseModel):
    """What the student sees after one turn."""

    action: Action
    feedback: str
    hint: str | None = None
    probe: str | None = None
    explanation: str | None = None
    examples: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class QuestionView(BaseModel):
    id: str
    concept_id: str
    concept_title: str
    type: str
    prompt: str
    hints_used: int
    hints_total: int


class SessionView(BaseModel):
    id: str
    status: str
    created_at: str
    updated_at: str
    content_id: str
    content_version: str
    content_title: str
    strategy_id: str
    strategy_version: str
    source: dict[str, Any] | None = None   # set when the questions were generated from a material range
    reading: ReadingView | None = None     # set for material sessions (導讀 happens before the questions)
    questions: list[dict[str, Any]] = Field(default_factory=list)   # id, concept_id, type, prompt — so records can show the question asked
    concepts: list[dict[str, Any]] = Field(default_factory=list)
    model_config_snapshot: dict[str, Any]
    state: dict[str, Any]
    current_question: QuestionView | None
    turns: list[dict[str, Any]]
    summary: dict[str, Any] | None
    workflow_runner: str = "legacy"            # which runner owns this session; fixed at creation
    revision: int = 0                          # business revision (events API expected_revision)
    workflow: dict[str, Any] | None = None     # langgraph sessions: {waiting, stage, active_command, error, …}


class EventRequest(BaseModel):
    """POST /api/sessions/{sid}/events (langgraph sessions)."""

    command_id: str | None = Field(default=None, max_length=80)
    type: str
    expected_revision: int | None = None
    unit_id: str | None = None
    question_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
