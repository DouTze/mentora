"""Contracts of the guided reading (reading-graph-v3): the task contract and what the reader has read, the 主題
overview, a 區塊's handout with its two reviews and its corrections, PDF symbol evidence, and the notes and
questions built from what was read.

What a model returns is a *proposal*: the program checks every quotation it offers against the texts before a
review verdict counts, and applies a correction only where the passage it names really is. Control information
(reading depth, reader background) steers generation but is never subject matter."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..llm.runtime import stable_hash

CONTRACT_VERSION = "reading-contracts-v2"

# -- T: task contract and learning state ------------------------------------------------------

class TaskContract(BaseModel):
    """What this study session is for. Stored in the session's workflow meta when it starts; `defaults` lists the
    fields the reader did not set (so a default is a recorded choice, not an assumption)."""
    scope: dict = Field(default_factory=dict)            # material, unit, start, end
    source_version: str = ""                              # snapshot id + extraction version
    goal: str = "讀懂指定範圍的內容，之後能說明其中的定義、主張、條件與推理"
    depth: Literal["close", "skim"] = "close"             # 細讀 / 略讀 (略讀 lowers teaching depth, never coverage)
    reader_background: str = ""                           # what the reader said about themselves; "" = not given
    constraints: dict = Field(default_factory=lambda: {"language": "zh-TW", "format": "markdown+katex"})
    defaults: list[str] = Field(default_factory=list)


class LearningSnapshot(BaseModel):
    """What the guide of one unit may assume about the reader. Generated ≠ presented ≠ read ≠ understood:
    - read_claims: key points of units the reader moved past ("next"); read, not proven understood;
    - open_questions: questions the reader asked on read units, with where they came from;
    - evidence: answers that showed understanding (assessments), with their source;
    a prefetched or generated guide the reader has not seen never appears here."""
    depth: Literal["close", "skim"] = "close"
    goal: str = ""
    background: str = ""
    read_claims: list[dict] = Field(default_factory=list)      # {unit_id, claim_id (K1 …), statement}
    open_questions: list[dict] = Field(default_factory=list)   # {unit_id, question}
    evidence: list[dict] = Field(default_factory=list)         # {concept, claim_ids, assessment}

    @property
    def hash(self) -> str:
        return stable_hash(self.model_dump())


# -- the 主題 overview ------------------------------------------------------------------------------

class TopicOverview(BaseModel):
    """reading_topic output: the 主題總摘要 (scope and context for each 區塊's guide, never content), the key points
    and the self-check question shown after the last 區塊."""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    check: str = ""


# -- reading-graph-v3: a teaching handout for one 區塊, reviewed and corrected in place ---------------------------

class HandoutDraft(BaseModel):
    """The 講義 of one 區塊: what a teacher hands out (or a self-learner reads) to learn what the textbook teaches."""
    guide: str = Field(min_length=10)
    key_points: list[str] = Field(default_factory=list)
    check: str = ""


class PointVerdict(BaseModel):
    id: str                                   # P1, P2 … (the 區塊's summary points)
    taught: Literal["yes", "no"]
    guide_quote: str = ""                     # the handout passage that teaches it (verbatim)


class PointsReview(BaseModel):
    """Review A: is each point the 區塊 summary says the textbook teaches taught by the handout?"""
    points: list[PointVerdict]


class Conflict(BaseModel):
    guide_quote: str                          # the handout passage (verbatim)
    source_quote: str                         # the textbook passage it contradicts (verbatim)
    why: str = ""
    both_true: Literal["yes", "no"]           # can the two passages both be true? "yes" = not a contradiction


class ConflictsReview(BaseModel):
    """Review B: where does the handout say something that cannot be true if the textbook is? Rewording, additions
    and simplifications can both be true with the textbook, so they are not conflicts."""
    conflicts: list[Conflict] = Field(default_factory=list)


class Edit(BaseModel):
    """One correction of the handout: replace a verbatim passage, or insert after one ("" = at the end)."""
    problem: str = ""                         # F1, F2 … of the problem list
    action: Literal["replace", "insert_after"]
    old: str = ""                             # replace: the passage to replace; insert_after: the anchor passage
    new: str


class HandoutFix(BaseModel):
    edits: list[Edit] = Field(default_factory=list)


# -- R5: symbol evidence from rendered PDF regions ------------------------------------------------

class SymbolItem(BaseModel):
    symbol: str                                    # as it appears (e.g. "w")
    latex: str                                     # KaTeX that keeps the visible form (e.g. "\mathbf{w}")
    style: Literal["bold", "italic", "upright", "blackboard", "calligraphic", "script", "fraktur", "sans", "other"]
    script: Literal["none", "superscript", "subscript"] = "none"


class SymbolTranscription(BaseModel):
    """What a vision-capable model reads from the rendered regions of one unit (never from the text layer)."""
    formulas: list[str] = Field(default_factory=list)   # KaTeX of each formula / expression, in reading order
    symbols: list[SymbolItem] = Field(default_factory=list)
    unreadable: list[str] = Field(default_factory=list) # what could not be read with confidence


# -- R6: notes and questions from verified content ----------------------------------------------

class NoteItem(BaseModel):
    """One note of a part, compressed from verified claims it cites ("U2.C3" = claim C3 of the part's 2nd unit)."""
    id: str = Field(pattern=r"^N\d+$")
    kind: Literal["point", "confusion", "term"] = "point"
    term: str = ""                                         # kind == term: the term
    text: str = Field(min_length=2)
    claim_refs: list[str] = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)


class NotesOutput(BaseModel):
    notes: list[NoteItem] = Field(min_length=1)


class NoteAuditItem(BaseModel):
    id: str
    verdict: Literal["faithful", "distorted", "unsupported", "unsure"]
    note: str = ""


class NotesAudit(BaseModel):
    notes: list[NoteAuditItem]


class VerifiedNote(BaseModel):
    id: str
    kind: str
    term: str = ""
    text: str
    claim_refs: list[str]
    unit_ids: list[str]
    conditions: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    status: Literal["verified", "unverified"]
    reasons: list[str] = Field(default_factory=list)


class QuizQuestion(BaseModel):
    prompt: str = Field(min_length=4)
    rubric: str = Field(min_length=4)
    hints: list[str] = Field(default_factory=list)
    basis: list[str] = Field(min_length=1)                 # basis ids the question and its rubric rest on


class QuizConcept(BaseModel):
    topic: str                                             # topic id from the program's coverage plan (T1 …)
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=4)
    examples: list[str] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    basis: list[str] = Field(min_length=1)
    diagnostic: QuizQuestion
    transfer: QuizQuestion


class QuizDraft(BaseModel):
    title: str = Field(min_length=1)
    concepts: list[QuizConcept] = Field(min_length=1)


class QuizAuditItem(BaseModel):
    id: str                                                # T1.diagnostic, T1.transfer …
    verdict: Literal["ok", "unanswerable", "wrong_condition", "outside_scope", "leaks_answer"]
    note: str = ""


class QuizAudit(BaseModel):
    questions: list[QuizAuditItem]
