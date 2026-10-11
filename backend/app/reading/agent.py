"""Guided-reading contracts shared by the reading subgraph and the learning graph: the task input of one guide, a
finding, quotation lookup, formula helpers and the prompt fields every reading prompt shares."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm.runtime import stable_hash
from .contracts import LearningSnapshot

READING_WORKFLOW_VERSION = "reading-graph-v3"
GUIDE_MAX_TOKENS = 2048
GUIDE_CONTEXT_TOKENS = 8192


class ReadingTaskInput(BaseModel):
    session_id: str
    unit_id: str
    index: int
    total: int
    label: str
    heading: str | None = None
    text: str                                 # rebuilt from the snapshot blocks by the program
    block_ids: list[str] = Field(default_factory=list)
    has_figure: bool = False
    material_title: str
    range_label: str
    prev_points: list[str] = Field(default_factory=list)
    qa_context: list[dict[str, Any]] = Field(default_factory=list)
    model_config_hash: str
    prompt_bundle_hash: str
    attempt: int = 1
    learning: LearningSnapshot = Field(default_factory=LearningSnapshot)
    source_kind: str = "md"                      # md | txt | pdf (decides what counts as symbol evidence)
    depends_on_prev: bool = False                # a protected relation (argument, example …) continues from the previous unit
    symbol_evidence: dict | None = None          # R5: page / region / font evidence of this unit, if any
    # a 區塊 of a 主題 (docs/reading-block-guide-plan.md): {"summary": 主題總摘要, "chunks": [{"topic", "aspect",
    # "summary"} …], "index": this 區塊}. The 區塊's summary is what its handout must teach; the rest is scope
    topic: dict | None = None
    language: str = "zh"                         # the session's output language: "zh" (繁體中文) or "src" (the source's own)
    # display formulas ("$$…$$") set elsewhere in the reading that this passage names by their number (an exercise
    # "prove the second equality in (3.10)"): shown with the passage so nothing about them is made up
    cited: list[str] = Field(default_factory=list)

    @property
    def source_hash(self) -> str:
        return stable_hash([self.unit_id, self.text, self.block_ids])

    @property
    def context_hash(self) -> str:
        """Everything about the reader and the writing the handout depends on."""
        return stable_hash([self.prev_points, [(q.get("question"), q.get("answer")) for q in self.qa_context],
                            self.learning.hash, self.language] + ([self.topic] if self.topic else []))


class Finding(BaseModel):
    """A problem of a handout. `check`: which review found it — "points" (a summary point not taught), "conflicts"
    (contradicts the textbook) or "program" (a program check). status: confirmed = the evidence stands (a program
    fact, or a reviewer claim whose quotations are really in the texts); unconfirmed = the reviewer's evidence did not
    check out, so it is only shown to the reader as a note and never blocks or triggers a correction."""
    code: str
    check: Literal["points", "conflicts", "program"]
    severity: Literal["major", "minor"]
    detail: str
    status: Literal["confirmed", "unconfirmed"] = "confirmed"
    item: str = ""                 # the summary point id (P1 …), if any
    source_excerpt: str = ""
    draft_excerpt: str = ""        # the handout passage the problem is about (what a correction replaces)
    suggested_fix: str = ""


def norm_text(text: str) -> str:
    """Whitespace- and width-insensitive form used to locate a quotation (not to judge meaning)."""
    t = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s*_`>]+", "", t)


def contains_quote(haystack: str, quote: str, min_chars: int = 2) -> bool:
    q = norm_text(quote).strip("….。，,;；:：「」\"'")
    return len(q) >= min_chars and q in norm_text(haystack)


def expand_quote(text: str, fragment: str, min_chars: int = 6) -> str:
    """The whole sentence(s) of `text` that contain the quoted fragment ("" if the fragment is not there). An
    auditor may quote a short locating fragment; strength markers and formulas are checked on the full sentence.
    A small model often merges two sentences into one quotation ("A，且 B"): then the quotation is cut at commas and
    sentence marks, a connective the auditor added at the start of a piece (且、並、而且、同時…) is dropped, every
    remaining piece — short ones included, so an inserted 「不」 cannot hide — must be verbatim in the text, and the
    evidence is the sentences holding the pieces."""
    from .gates import source_segments
    sents = [x for _, x in source_segments(text)]

    def holding(q: str) -> list[int]:
        return [k for k, x in enumerate(sents) if q in norm_text(x)]

    if contains_quote(text, fragment, min_chars):
        q = norm_text(fragment).strip("….。，,;；:：「」\"'")
        for n in range(1, len(sents) + 1):
            for i in range(len(sents) - n + 1):
                joined = " ".join(sents[i:i + n])
                if q in norm_text(joined):
                    return joined
        return fragment
    pieces = [re.sub(r"^(?:並且|而且|以及|同時|且|並|和|及|而)", "", norm_text(x).strip("，,:：「」\"'"))
              for x in re.split(r"[，,。！？；;!?]|…|\.\.\.", fragment or "")]
    pieces = [x for x in pieces if x]
    if len(pieces) < 2 or sum(len(x) for x in pieces) < min_chars:
        return ""
    found = [holding(x) for x in pieces]
    if not all(found):
        return ""
    return " ".join(sents[k] for k in sorted({ks[0] for ks in found}))


# -- formulas -------------------------------------------------------------------------

def _norm_math(s: str) -> str:
    """Cosmetic KaTeX differences only: spacing commands, \\left/\\right, \\dfrac/\\tfrac, braces around one token."""
    t = re.sub(r"\\[,;:!]|\\(?:left|right|displaystyle)\b|\s+", "", s or "")
    t = re.sub(r"\\[dt]frac", r"\\frac", t)
    return re.sub(r"([_^])\{(\\[A-Za-z]+|[A-Za-z0-9])\}", r"\1\2", t)


EQ_REF_RE = re.compile(r"\((\d+(?:\.\d+)*[a-z]?)\)")   # an equation named by its number in the text: "(3.10)"
EQ_TAG_RE = re.compile(r"\\tag\{([^{}]*)\}")
CITED_MAX = 3


def cited_formulas(text: str, others: list[str]) -> list[str]:
    """The display formulas ("$$…$$", from the texts `others`) whose equation numbers `text` names but does not set
    itself, in the order it names them."""
    own = set(EQ_TAG_RE.findall(text or ""))
    named = [n for n in EQ_REF_RE.findall(re.sub(r"\$\$.+?\$\$", " ", text or "", flags=re.S)) if n not in own]
    pool: dict[str, str] = {}
    for o in others:
        for d in re.findall(r"\$\$.+?\$\$", o or "", re.S):
            for n in EQ_TAG_RE.findall(d):
                pool.setdefault(n, d)
    return list(dict.fromkeys(pool[n] for n in named if n in pool))[:CITED_MAX]


def source_formulas(text: str) -> tuple[list[str], list[str]]:
    display = re.findall(r"\$\$(.+?)\$\$", text or "", re.S)
    rest = re.sub(r"\$\$.+?\$\$", " ", text or "", flags=re.S)
    inline = re.findall(r"(?<!\$)\$([^$\n]+?)\$(?!\$)", rest)
    return [_norm_math(d) for d in display if d.strip()], [_norm_math(i) for i in inline if i.strip()]


def _is_real_math(f: str) -> bool:
    return any(ch in f for ch in "\\=^_<>+-*/") or len(f) > 3


# -- prompt fields -------------------------------------------------------------------

def qa_text(qa: list[dict]) -> str:
    return "\n".join(f"- 第 {q.get('quote_index', 0) + 1} 段，學生問：{q.get('question')}" for q in qa[-6:]) or "（無）"


def task_fields(t: ReadingTaskInput) -> dict[str, str]:
    heading = t.heading or "（無）"
    text = t.text + ("\n\n（注意：這一段含有圖，但圖片內容沒有被抽取出來，只能依文字說明。）" if t.has_figure else "")
    if t.cited:
        text += "\n\n（這一段提到的公式寫在課文別處，原式如下，只供對照：）\n" + "\n".join(t.cited)
    return {"material_title": t.material_title, "range_label": t.range_label, "position": f"{t.index + 1}/{t.total}",
            "quote_label": t.label, "heading": heading, "quote_text": text, "unit_id": t.unit_id,
            "prev_points": "\n".join(f"- {p}" for p in t.prev_points) or "（這是第一段）",
            "qa_context": qa_text(t.qa_context)}
