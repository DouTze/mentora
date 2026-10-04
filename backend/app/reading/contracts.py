"""Contracts of the source-first guided reading (R3), after WaAi-ML ReadingAgent §6:

    task contract (T) → source claims + 中文課文 (A) → Gate 1: notation (B) + full-text coverage (C)
    → learning goal, obstacles and approved bridges (D) → 主要正文 (E) → Gate 2 (F) → publish

What a model returns is a *proposal*; the program anchors it to the source, checks it and records the result as
a GateResult with the expected checks, what came back and the evidence. An empty list, a missing item or a schema-
valid object is never read as "all checks passed".

Control information (reading depth, reader background, obstacles, the reasons for a bridge) steers generation but
is never subject matter: only claims anchored in the source and the `content` of approved bridges may become
sentences of the 主要正文."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..llm.runtime import stable_hash

CONTRACT_VERSION = "reading-contracts-v1"

ClaimFunction = Literal["definition", "claim", "condition", "exception", "example", "counterexample", "comparison",
                        "reasoning", "formula", "warning", "procedure", "question", "transition", "other"]
CheckStatus = Literal["pass", "fail", "unverified", "not_applicable"]


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
    - read_claims: verified claims of units the reader moved past ("next"); read, not proven understood;
    - open_questions: questions the reader asked on read units, with where they came from;
    - evidence: answers that showed understanding (assessments), with their source;
    a prefetched or generated guide the reader has not seen never appears here."""
    depth: Literal["close", "skim"] = "close"
    goal: str = ""
    background: str = ""
    read_claims: list[dict] = Field(default_factory=list)      # {unit_id, claim_id, statement}
    open_questions: list[dict] = Field(default_factory=list)   # {unit_id, question}
    evidence: list[dict] = Field(default_factory=list)         # {concept, claim_ids, assessment}

    @property
    def hash(self) -> str:
        return stable_hash(self.model_dump())


# -- A: source claims ---------------------------------------------------------------------------

class SourceClaim(BaseModel):
    """A proposition of the source. It cites the program-numbered sentences it rests on (S1, S2 …); `quote` is a
    verbatim sub-span only when it covers part of a sentence. `source_text` is resolved by the program after
    anchoring (never taken from the model) and is what later stages compare against."""
    id: str = Field(pattern=r"^C\d+$")
    sentences: list[str] = Field(min_length=1)
    quote: str = ""
    statement: str = ""          # the proposition in 繁體中文 when it is not simply the cited text (resolved if empty)
    function: ClaimFunction = "claim"
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    strength: str = ""                                   # negation / necessity / possibility / quantifier, if any
    formulas: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    source_text: str = ""


class SentenceTranslation(BaseModel):
    id: str                       # S1, S2 … (program-numbered source sentence)
    zh: str = Field(min_length=1)


class SourceUnderstanding(BaseModel):
    """understand / revise_source output: for a non-Chinese source one faithful translation per numbered sentence
    (the program assembles the 中文課文 from them, so every sentence is aligned), and the claims in source order.
    `chinese_text` is filled by the program, never by the model."""
    translations: list[SentenceTranslation] = Field(default_factory=list)
    chinese_text: str | None = None
    claims: list[SourceClaim] = Field(min_length=1)
    needs_context: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


class SentenceAudit(BaseModel):
    id: str
    verdict: Literal["faithful", "missing", "distorted", "unsure"]
    note: str = ""


class ClaimAudit(BaseModel):
    id: str
    verdict: Literal["faithful", "distorted", "unsure"]
    note: str = ""


class SourceAudit(BaseModel):
    """source_audit output: one verdict for every expected source sentence (translation) and listed claim."""
    sentences: list[SentenceAudit]
    claims: list[ClaimAudit]


# -- gate results -------------------------------------------------------------------------------

class GateCheck(BaseModel):
    id: str                                   # e.g. coverage:S3, claim:C2, notation, chinese_text:formula
    kind: str
    status: CheckStatus
    detail: str = ""
    evidence: str = ""
    fixable: bool = True                      # a revision can fix it (missing evidence cannot)


class GateResult(BaseModel):
    gate: Literal["gate1", "gate2"]
    status: Literal["pass", "fail", "unverified"]
    expected: list[str]                       # every check that had to report
    checks: list[GateCheck]
    version: str = CONTRACT_VERSION

    @classmethod
    def of(cls, gate: str, checks: list[GateCheck]) -> "GateResult":
        st = [c.status for c in checks]
        status = "fail" if "fail" in st else "unverified" if "unverified" in st else "pass"
        if not checks:
            status = "unverified"
        return cls(gate=gate, status=status, expected=[c.id for c in checks], checks=checks)

    def failing(self) -> list[GateCheck]:
        return [c for c in self.checks if c.status in ("fail", "unverified")]


class VerifiedSource(BaseModel):
    """Gate 1 passed: the claims and 中文課文 later stages may rely on, and what they depend on."""
    key: str                                  # cache key (source, context, evidence, prompts, model, versions)
    unit_id: str
    source_hash: str
    chinese_text: str | None
    claims: list[SourceClaim]
    gate1: GateResult
    supplement_ids: list[str] = Field(default_factory=list)
    shareable: bool = True                    # no session data in its inputs (may be reused across sessions)
    limits: list[str] = Field(default_factory=list)
    version: str = CONTRACT_VERSION


# -- D: bridges ---------------------------------------------------------------------------------

class BridgeSpec(BaseModel):
    id: str = Field(pattern=r"^B\d+$")
    obstacle: str = Field(min_length=2)        # internal: the concrete obstacle (never written into the guide)
    targets: list[str] = Field(min_length=1)   # claim ids it serves
    content: str = Field(min_length=2)         # the approved subject matter to add — the only new content allowed
    basis: Literal["source", "derivation", "prior_reading", "prerequisite", "user_authorized"]
    boundary: Literal["integrated", "supplement"] = "integrated"   # woven in / a labelled 補充說明 paragraph
    rationale: str = ""                        # internal: why removing it would hurt understanding


class BridgePlan(BaseModel):
    learning_goal: str = ""
    bridges: list[BridgeSpec] = Field(default_factory=list)       # empty = the source suffices


# -- E / F: body and Gate 2 ----------------------------------------------------------------------

class BodyDraft(BaseModel):
    guide: str = Field(min_length=10)
    key_points: list[str] = Field(default_factory=list)
    check: str = ""
    bridges_used: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


class ClaimCoverage(BaseModel):
    id: str
    verdict: Literal["covered", "missing", "distorted", "unsure"]
    guide_quote: str = ""
    note: str = ""


class BridgeReview(BaseModel):
    id: str
    verdict: Literal["ok", "absent", "incorrect", "unnecessary"]
    guide_quote: str = ""
    note: str = ""


class Addition(BaseModel):
    guide_quote: str
    kind: Literal["unsupported", "incorrect", "process", "narration", "redirect"]
    note: str = ""


class BodyAudit(BaseModel):
    claims: list[ClaimCoverage]
    bridges: list[BridgeReview]
    additions: list[Addition]
    uncovered_source: list[str]               # source passages (verbatim) the guide misses, beyond the claim list
    coherence: Literal["pass", "fail"]
    coherence_note: str = ""


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
