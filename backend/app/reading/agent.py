"""Guided-reading agent contracts (plan §5.2): task input, teaching plan, draft, review, artifact; the program-side
validation of a draft and the pass / revise / fail decision.

The model reviewer is a quality signal: its issues are mapped to a severity by *code* here (a model cannot
downgrade a formula error to a style remark), program validation runs first and independently, and only the
program decides whether a draft is published."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm.runtime import stable_hash
from ..tutor.reading import is_chinese

READING_WORKFLOW_VERSION = "reading-graph-v1"
GUIDE_MAX_TOKENS = 2048
GUIDE_CONTEXT_TOKENS = 8192
SUPPLEMENT_CONTEXT_TOKENS = 12288

MAJOR = {"wrong_formula", "missing_condition", "unsupported_claim", "missing_key_claim", "meaning_changed",
         "followed_injection", "broken_math", "missing_chinese_text", "empty_guide"}
MINOR = {"narration", "unclear", "verbose", "formula_paraphrased", "short_guide", "review_unverified", "supplement_flagged"}
MISSING_CODES = {"missing_condition", "missing_key_claim"}
DRAFT_CODES = {"unsupported_claim", "meaning_changed", "wrong_formula", "followed_injection"}
COVERED = 0.8   # share of the cited source's character bigrams already present in the draft
REDIRECT_RE = re.compile(r"請參考原文|回去看原文|原文有更詳細|詳見原文|請見原文|如上所述|前面的對照已經說明")
# a unit that points at earlier text within its first words ("在這兩個條件下", "上述方法", "These results")
REFERS_BACK_RE = re.compile(r"^\W{0,2}.{0,6}?(這兩|這三|這些|這個|這種|此一|上述|前述|以上|它們|同樣地|另一方面|第二個|these|this|such|the above|similarly)", re.I)


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

    @property
    def source_hash(self) -> str:
        return stable_hash([self.unit_id, self.text, self.block_ids])

    @property
    def context_hash(self) -> str:
        return stable_hash([self.prev_points, [(q.get("question"), q.get("answer")) for q in self.qa_context]])


class TeachingPlan(BaseModel):
    definitions: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    needs_context: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


class GuideDraft(BaseModel):
    chinese_text: str | None = None
    guide: str = Field(min_length=10)
    key_points: list[str] = Field(default_factory=list)
    check: str = ""
    supplement_notes: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


IssueCode = Literal["wrong_formula", "missing_condition", "unsupported_claim", "missing_key_claim", "meaning_changed",
                    "followed_injection", "narration", "unclear", "verbose"]


class ReviewIssue(BaseModel):
    code: IssueCode
    severity: Literal["major", "minor"] = "minor"
    source_excerpt: str = ""
    draft_excerpt: str = ""
    suggested_fix: str = ""


class ReviewResult(BaseModel):
    issues: list[ReviewIssue] = Field(default_factory=list)


class VerifyCheck(BaseModel):
    item: int
    covered: bool = False
    draft_quote: str = ""


class VerifyResult(BaseModel):
    checks: list[VerifyCheck] = Field(default_factory=list)


def to_verify(findings: list["Finding"]) -> list[int]:
    """Indices of review findings the extraction check can settle: blocking "missing" claims from the reviewer."""
    return [i for i, f in enumerate(findings) if f.origin == "review" and f.severity == "major" and f.code in MISSING_CODES]


def apply_verify(findings: list["Finding"], idx: list[int], v: VerifyResult, draft: GuideDraft | dict) -> list["Finding"]:
    """A "missing" claim is withdrawn only when the model copies a sentence that really is in the draft."""
    d = draft.model_dump() if isinstance(draft, BaseModel) else draft
    body = (d.get("guide") or "") + "\n" + (d.get("chinese_text") or "")
    out = list(findings)
    for c in v.checks:
        k = c.item - 1
        if 0 <= k < len(idx) and c.covered and len(c.draft_quote.strip()) >= 6 and coverage(c.draft_quote, body) >= 0.9:
            f = out[idx[k]]
            out[idx[k]] = f.model_copy(update={"code": "review_unverified", "severity": "minor",
                                                "detail": f.detail + f"（核對：草稿已寫到「{c.draft_quote[:60]}」）"})
    return out


class Finding(BaseModel):
    """A validation or review finding with the severity the program assigned."""
    code: str
    severity: Literal["major", "minor"]
    detail: str
    origin: Literal["program", "review"]
    source_excerpt: str = ""
    draft_excerpt: str = ""
    suggested_fix: str = ""


def severity_of(code: str) -> Literal["major", "minor"]:
    return "major" if code in MAJOR else "minor"


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[\s，。、；：！？,.;:!?「」『』（）()\"'*`$]+", "", text or "").lower()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def coverage(excerpt: str, draft: str) -> float:
    """Share of the excerpt's character bigrams that also occur in the draft (1.0 = all there)."""
    b = _bigrams(excerpt)
    return len(b & _bigrams(draft)) / len(b) if b else 0.0


def _in_supplement(excerpt: str, guide: str) -> bool:
    for para in re.split(r"\n\s*\n", guide or ""):
        if para.lstrip("*_ ").startswith("補充說明") and coverage(excerpt, para) >= COVERED:
            return True
    return False


def review_findings(r: ReviewResult, draft: GuideDraft | dict | None = None) -> list[Finding]:
    """Review issues with the severity the program assigns. A small reviewer often cites the whole paragraph as
    "missing" while the draft repeats it, or quotes draft text that is not there; such claims are checked against
    the texts and kept as `review_unverified` reminders instead of blocking publication. A flagged passage inside a
    labelled 補充說明 paragraph becomes `supplement_flagged` (recorded, shown to reviewers, not blocking)."""
    d = draft.model_dump() if isinstance(draft, BaseModel) else (draft or {})
    body = (d.get("guide") or "") + "\n" + (d.get("chinese_text") or "")
    out = []
    for i in r.issues:
        code, note = i.code, ""
        if draft is not None:
            if i.code in MISSING_CODES and (not i.source_excerpt.strip() or coverage(i.source_excerpt, body) >= COVERED):
                code, note = "review_unverified", f"（檢查稱 {i.code}，但引用的原文內容已在草稿中）"
            elif i.code in DRAFT_CODES and i.draft_excerpt.strip() and coverage(i.draft_excerpt, body) < COVERED:
                code, note = "review_unverified", f"（檢查稱 {i.code}，但引用的草稿片段不在草稿中）"
            elif i.code == "unsupported_claim" and _in_supplement(i.draft_excerpt, d.get("guide") or ""):
                code, note = "supplement_flagged", "（標示為補充說明的內容被檢查指出）"
        out.append(Finding(code=code, severity=severity_of(code), detail=(i.suggested_fix or i.code) + note,
                           origin="review", source_excerpt=i.source_excerpt, draft_excerpt=i.draft_excerpt,
                           suggested_fix=i.suggested_fix))
    return out


# -- program validation -------------------------------------------------------------

def _norm_math(s: str) -> str:
    return re.sub(r"\s+", "", s)


def source_formulas(text: str) -> tuple[list[str], list[str]]:
    display = re.findall(r"\$\$(.+?)\$\$", text, re.S)
    rest = re.sub(r"\$\$.+?\$\$", " ", text, flags=re.S)
    inline = re.findall(r"(?<!\$)\$([^$\n]+?)\$(?!\$)", rest)
    return [_norm_math(d) for d in display if d.strip()], [_norm_math(i) for i in inline if i.strip()]


def validate_draft(task: ReadingTaskInput, d: GuideDraft) -> list[Finding]:
    out: list[Finding] = []
    body = (d.guide or "") + "\n" + (d.chinese_text or "")
    bn = _norm_math(body)
    if not d.guide.strip() or len(d.guide.strip()) < 10:
        out.append(Finding(code="empty_guide", severity="major", detail="主要正文是空的", origin="program"))
    if not is_chinese(task.text) and not (d.chinese_text or "").strip():
        out.append(Finding(code="missing_chinese_text", severity="major",
                           detail="原文不是中文，chinese_text 必須是忠實的中文課文", origin="program"))
    display, inline = source_formulas(task.text)
    for f in display:
        if f not in bn:
            out.append(Finding(code="wrong_formula", severity="major", detail=f"獨立公式沒有原樣保留：$${f}$$",
                               origin="program", source_excerpt=f))
    for f in inline:
        if f not in bn:
            real = "\\" in f or "=" in f or "^" in f or "_" in f
            out.append(Finding(code="wrong_formula" if real else "formula_paraphrased", severity="major" if real else "minor",
                               detail=f"行內公式沒有原樣保留：${f}$", origin="program", source_excerpt=f))
    if (d.guide.count("$") - 2 * d.guide.count("$$")) % 2 or d.guide.count("$$") % 2:
        out.append(Finding(code="broken_math", severity="major", detail="正文的 $ 數量不成對，公式會無法顯示", origin="program"))
    m = REDIRECT_RE.search(body)
    if m:
        out.append(Finding(code="narration", severity="minor", detail=f"不可要求學生回看原文：「{m.group(0)}」", origin="program"))
    if len(d.guide) < min(80, len(task.text) * 0.3):
        out.append(Finding(code="short_guide", severity="minor", detail="正文明顯短於原文，可能漏掉內容", origin="program"))
    return out


def decide(findings: list[Finding], *, revised: bool, can_revise: bool) -> Literal["pass", "revise", "fail"]:
    majors = [f for f in findings if f.severity == "major"]
    if not majors:
        return "pass"
    return "revise" if (not revised and can_revise) else "fail"


CONTEXT_HINT_RE = re.compile(r"前文|前一段|前面|上一段|上述|前述|後文|下一段|指的是|指涉|this refers|previous|earlier")


def needs_context(task: ReadingTaskInput, plan: TeachingPlan) -> bool:
    """Context selection is a model call; only spend it when the unit points elsewhere — the text opens with a
    referring phrase, or the analysis names earlier / later text (a small analyzer lists "needs context" for
    almost every unit, so a bare entry is not enough)."""
    return bool(REFERS_BACK_RE.match(task.text)) or any(CONTEXT_HINT_RE.search(x) for x in plan.needs_context)


# -- prompt fields -------------------------------------------------------------------

def plan_text(p: TeachingPlan | dict | None) -> str:
    if p is None:
        return "（無）"
    d = p.model_dump() if isinstance(p, BaseModel) else p
    names = {"definitions": "定義", "claims": "主張與結論", "conditions": "條件、前提、例外", "formulas": "公式與符號",
             "reasoning": "推理鏈", "prerequisites": "先備概念（可補充說明）", "limits": "無法從文字得知、必須如實說明"}
    lines = []
    for k, title in names.items():
        if d.get(k):
            lines.append(f"- {title}：" + "；".join(d[k]))
    return "\n".join(lines) or "（無）"


def qa_text(qa: list[dict]) -> str:
    return "\n".join(f"- 第 {q.get('quote_index', 0) + 1} 段，學生問：{q.get('question')}" for q in qa[-6:]) or "（無）"


def task_fields(t: ReadingTaskInput) -> dict[str, str]:
    from ..tutor.reading import chinese_text_rule
    heading = t.heading or "（無）"
    text = t.text + ("\n\n（注意：這一段含有圖，但圖片內容沒有被抽取出來，只能依文字說明。）" if t.has_figure else "")
    return {"material_title": t.material_title, "range_label": t.range_label, "position": f"{t.index + 1}/{t.total}",
            "quote_label": t.label, "heading": heading, "quote_text": text, "unit_id": t.unit_id,
            "prev_points": "\n".join(f"- {p}" for p in t.prev_points) or "（這是第一段）",
            "qa_context": qa_text(t.qa_context), "chinese_text_rule": chinese_text_rule({"text": t.text})}


def findings_text(fs: list[Finding]) -> str:
    return "\n".join(f"- [{f.code}] {f.detail}" + (f"（原文：{f.source_excerpt}）" if f.source_excerpt else "")
                     + (f"（草稿：{f.draft_excerpt}）" if f.draft_excerpt else "") for f in fs)


def draft_json(d: GuideDraft | dict) -> str:
    return json.dumps(d.model_dump() if isinstance(d, BaseModel) else d, ensure_ascii=False)
