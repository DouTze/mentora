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
    backend: Literal["mock", "ollama", "openvino"] = "mock"
    model: str = ""
    host: str = ""          # ollama host
    model_path: str = ""    # openvino model dir
    device: str = "GPU"     # openvino device
    temperature: float = 0.2
    max_tokens: int = 512
    context_tokens: int = 8192


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


class CategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class MaterialUpdate(BaseModel):
    title: str | None = None
    category_id: str | None = None


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
