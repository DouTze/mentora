"""Guided-reading agent contracts (plan §5.2): task input, teaching plan, draft, review, artifact; the program-side
validation of a draft and the pass / revise / fail decision.

The model reviewer is a quality signal: its issues are mapped to a severity by *code* here (a model cannot
downgrade a formula error to a style remark), program validation runs first and independently, and only the
program decides whether a draft is published."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm.runtime import stable_hash
from ..tutor.reading import is_chinese
from .contracts import LearningSnapshot
from .markers import parity

READING_WORKFLOW_VERSION = "reading-graph-v2"
GUIDE_MAX_TOKENS = 2048
GUIDE_CONTEXT_TOKENS = 8192
SUPPLEMENT_CONTEXT_TOKENS = 12288

MAJOR = {"wrong_formula", "missing_condition", "unsupported_claim", "missing_key_claim", "meaning_changed",
         "followed_injection", "broken_math", "missing_chinese_text", "empty_guide", "formula_not_verbatim",
         "depends_on_chinese_text", "redirect_to_source", "process_leak", "review_failed_check",
         "coverage_unconfirmed", "bridge_missing", "bridge_incorrect", "unapproved_supplement", "incoherent",
         "bridge_unconfirmed", "bridge_correctness_unconfirmed",
         "context_missing"}
MINOR = {"narration", "unclear", "verbose", "formula_paraphrased", "short_guide", "review_withdrawn", "formula_rewritten",
         "bridge_checked_by_model",
         "bridge_questioned", "review_citation_invalid", "context_outside_range"}
MISSING_CODES = {"missing_condition", "missing_key_claim"}
DRAFT_CODES = {"unsupported_claim", "meaning_changed", "wrong_formula", "followed_injection"}
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
    learning: LearningSnapshot = Field(default_factory=LearningSnapshot)
    source_kind: str = "md"                      # md | txt | pdf (decides what counts as symbol evidence)
    depends_on_prev: bool = False                # a protected relation (argument, example …) continues from the previous unit
    symbol_evidence: dict | None = None          # R5: page / region / font evidence of this unit, if any

    @property
    def source_hash(self) -> str:
        return stable_hash([self.unit_id, self.text, self.block_ids])

    @property
    def context_hash(self) -> str:
        """Everything about the reader the guide depends on (the verified source does not depend on it)."""
        return stable_hash([self.prev_points, [(q.get("question"), q.get("answer")) for q in self.qa_context],
                            self.learning.hash])


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
Target = Literal["guide", "chinese_text"]

# What every review must explicitly report on (R1). A review without a result for each expected check is not a
# review: it goes back once as a format error and otherwise ends as `review_invalid` (never as "no issues").
GUIDE_CHECKS = {
    "guide_coverage": "主要正文本身涵蓋原文所有定義、主張、例子、推理與結論（不看中文課文）",
    "guide_conditions": "主要正文保留原文的條件、例外、否定、量詞與命題力度（不看中文課文）",
    "guide_formulas": "主要正文的公式與符號和原文一致（不看中文課文）",
    "guide_additions": "主要正文新增的內容（含「補充說明」）正確、有依據且必要",
}
CHINESE_TEXT_CHECK = {"chinese_text": "中文課文忠實、完整，沒有增刪或改變命題力度"}


def expected_checks(has_chinese_text: bool) -> dict[str, str]:
    return {**GUIDE_CHECKS, **(CHINESE_TEXT_CHECK if has_chinese_text else {})}


class ReviewIssue(BaseModel):
    code: IssueCode
    target: Target
    severity: Literal["major", "minor"] = "minor"
    source_excerpt: str = ""
    draft_excerpt: str = ""
    suggested_fix: str = ""


class ReviewCheck(BaseModel):
    check: str
    result: Literal["pass", "fail"]
    evidence: str = ""


class ReviewResult(BaseModel):
    checks: list[ReviewCheck] = Field(min_length=1)
    issues: list[ReviewIssue]


def review_check(expected: dict[str, str]):
    """Program check for run_structured: each expected check reported exactly once (else one format fix)."""
    from ..llm.tasks import OutputInvalid

    def check(r: ReviewResult) -> None:
        got = [c.check for c in r.checks]
        missing = [k for k in expected if k not in got]
        extra = sorted({k for k in got if k not in expected})
        dup = sorted({k for k in got if got.count(k) > 1})
        if missing or extra or dup:
            raise OutputInvalid("checks 必須對下列每一項各回報一次 pass 或 fail："
                                + "、".join(expected) + (f"；缺少 {missing}" if missing else "")
                                + (f"；不存在的項目 {extra}" if extra else "") + (f"；重複 {dup}" if dup else ""))
    return check


class VerifyCheck(BaseModel):
    item: int
    verdict: Literal["problem", "no_problem"]
    guide_quote: str = ""
    reason: str = ""


class VerifyResult(BaseModel):
    checks: list[VerifyCheck] = Field(min_length=1)


class Finding(BaseModel):
    """A validation or review finding with the severity the program assigned.

    status: confirmed = the evidence stands (program fact, or a reviewer claim whose citations check out);
    unconfirmed = a major claim the program could not tie to the texts (invalid citation, a formula that is not
    verbatim but may be an equivalent rewrite, a strength marker that looks lost). Unconfirmed majors still block
    publication until a focused re-check (verify) or a revision settles them."""
    code: str
    severity: Literal["major", "minor"]
    detail: str
    origin: Literal["program", "review", "verify"]
    target: Literal["guide", "chinese_text", "source"] = "guide"
    status: Literal["confirmed", "unconfirmed"] = "confirmed"
    item: str = ""                 # the claim / bridge id the finding is about (Gate 2), if any
    source_excerpt: str = ""
    draft_excerpt: str = ""
    suggested_fix: str = ""


def severity_of(code: str) -> Literal["major", "minor"]:
    return "major" if code in MAJOR else "minor"


def norm_text(text: str) -> str:
    """Whitespace- and width-insensitive form used to locate a quotation (not to judge meaning)."""
    t = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s*_`>]+", "", t)


def contains_quote(haystack: str, quote: str, min_chars: int = 2) -> bool:
    q = norm_text(quote).strip("….。，,;；:：「」\"'")
    return len(q) >= min_chars and q in norm_text(haystack)


def expand_quote(text: str, fragment: str, min_chars: int = 6) -> str:
    """The whole sentence(s) of `text` that contain the quoted fragment ("" if the fragment is not there). An
    auditor may quote a short locating fragment; strength markers and formulas are checked on the full sentence."""
    if not contains_quote(text, fragment, min_chars):
        return ""
    from .gates import source_segments
    q = norm_text(fragment).strip("….。，,;；:：「」\"'")
    sents = [x for _, x in source_segments(text)]
    for n in range(1, len(sents) + 1):
        for i in range(len(sents) - n + 1):
            joined = " ".join(sents[i:i + n])
            if q in norm_text(joined):
                return joined
    return fragment


def target_text(d: dict, target: str) -> str:
    return (d.get("chinese_text") or "") if target == "chinese_text" else (d.get("guide") or "")


VERIFIABLE = MISSING_CODES | {"meaning_changed", "wrong_formula", "formula_not_verbatim", "unsupported_claim",
                              "followed_injection", "review_failed_check", "coverage_unconfirmed", "bridge_unconfirmed",
                              "bridge_correctness_unconfirmed"}


def review_findings(r: ReviewResult, draft: GuideDraft | dict, source: str) -> list[Finding]:
    """Review issues with the severity the program assigns. Each issue names the field it is about; its citations
    are looked up only in the source and in *that* field, so text in the 中文課文 never answers a claim about the
    主要正文. A citation that is not where the reviewer says makes the issue `unconfirmed` (it still blocks, and is
    re-checked) — neither "the draft is fine" nor "the draft is wrong" follows from an invalid citation."""
    d = draft.model_dump() if isinstance(draft, BaseModel) else draft
    out = []
    for i in r.issues:
        field = target_text(d, i.target)
        notes = []
        if i.source_excerpt.strip() and not contains_quote(source, i.source_excerpt):
            notes.append("引用的原文不在原文中")
        if not i.source_excerpt.strip() and not i.draft_excerpt.strip():
            notes.append("沒有引用任何原文或草稿")
        elif i.code in MISSING_CODES and not i.source_excerpt.strip():
            notes.append("沒有指出漏掉的原文")
        if i.code in DRAFT_CODES and i.draft_excerpt.strip() and not contains_quote(field, i.draft_excerpt):
            notes.append("引用的草稿片段不在" + ("中文課文" if i.target == "chinese_text" else "主要正文"))
        sev = severity_of(i.code)
        out.append(Finding(code=i.code, severity=sev, detail=(i.suggested_fix or i.code) + (f"（{'；'.join(notes)}）" if notes else ""),
                           origin="review", target=i.target, status="unconfirmed" if notes and sev == "major" else "confirmed",
                           source_excerpt=i.source_excerpt, draft_excerpt=i.draft_excerpt, suggested_fix=i.suggested_fix))
    flagged = {f.target for f in out if f.severity == "major"}
    for c in r.checks:   # a failed check must not disappear because the reviewer listed no issue for it
        target = "chinese_text" if c.check == "chinese_text" else "guide"
        if c.result == "fail" and target not in flagged:
            out.append(Finding(code="review_failed_check", severity="major", origin="review", target=target,
                               detail=f"檢查項目 {c.check} 未通過：{c.evidence or '（未說明）'}", suggested_fix=c.evidence))
    return out


def to_verify(findings: list[Finding]) -> list[int]:
    """Blocking findings a focused re-check may settle: reviewer claims, unconfirmed program flags."""
    return [k for k, f in enumerate(findings) if f.severity == "major" and f.code in VERIFIABLE
            and (f.origin == "review" or f.status == "unconfirmed")]


def verify_items_text(findings: list[Finding], idx: list[int]) -> str:
    lines = []
    for k, i in enumerate(idx, 1):
        f = findings[i]
        where = "中文課文" if f.target == "chinese_text" else "主要正文"
        lines.append(f"{k}. [{f.code}｜{where}] {f.detail}" + (f"（原文：{f.source_excerpt}）" if f.source_excerpt else "")
                     + (f"（被指出的片段：{f.draft_excerpt}）" if f.draft_excerpt else ""))
    return "\n".join(lines)


def apply_verify(findings: list[Finding], idx: list[int], v: VerifyResult, draft: GuideDraft | dict, source: str) -> list[Finding]:
    """A blocking finding is withdrawn only with evidence the program can check:
    - "missing" / strength / formula claims: the re-check copies the passage of the *same field* that covers it,
      the passage is really there, keeps every strength marker of the cited source (markers.parity) and every
      formula of the cited source;
    - a claim about a draft passage that is not in the draft (phantom citation): the re-check finds no problem.
    A reviewer's well-cited "unsupported" or "meaning changed" claim is not withdrawn by a re-check alone; it needs
    a revision. Everything else stays as it was (a re-check saying "problem" confirms it)."""
    d = draft.model_dump() if isinstance(draft, BaseModel) else draft
    out = list(findings)
    for c in v.checks:
        k = c.item - 1
        if not 0 <= k < len(idx):
            continue
        f = out[idx[k]]
        if c.verdict == "problem":
            out[idx[k]] = f.model_copy(update={"status": "confirmed", "detail": f.detail + (f"（核對：{c.reason[:80]}）" if c.reason else "")})
            continue
        field = target_text(d, f.target)
        src = f.source_excerpt if f.source_excerpt and contains_quote(source, f.source_excerpt) else ""
        phantom = f.code in DRAFT_CODES and f.draft_excerpt and not contains_quote(field, f.draft_excerpt)
        why = ""
        if phantom:
            ok = True
        elif f.code in ("unsupported_claim", "followed_injection", "meaning_changed") and f.status == "confirmed":
            ok, why = False, "有效引用的新增／改義問題需經修訂處理"
        elif f.code == "bridge_unconfirmed":   # the guide passage must exist and carry the bridge's content
            from .gates import SUPPLEMENT_MATCH, ra_overlap
            full = expand_quote(field, c.guide_quote, 6)
            ok = bool(full) and ra_overlap(f.source_excerpt, full) >= SUPPLEMENT_MATCH
            why = "" if ok else "沒有引出寫進這項橋梁內容的正文句子"
        elif f.code == "bridge_correctness_unconfirmed":   # a focused second judgement with its reason, recorded as such
            ok = bool(c.reason.strip())
            why = "" if ok else "沒有說明判斷依據"
        elif f.code == "formula_not_verbatim":   # the guide's own (rewritten) formula must be quoted from the guide
            ok = contains_quote(field, c.guide_quote, 3) and "$" in c.guide_quote
            why = "" if ok else "沒有從主要正文複製出改寫後的公式"
        else:
            full = expand_quote(field, c.guide_quote, 6)
            quoted = bool(full)
            lost = parity(src, full) if src and quoted else []
            dmath, imath = source_formulas(src)
            qn = _norm_math(full)
            missing_math = [m for m in dmath + imath if _norm_math(m) not in qn]
            ok = quoted and not lost and not missing_math and bool(src)
            why = ("引用的句子不在" + ("中文課文" if f.target == "chinese_text" else "主要正文") if not quoted else
                   "；".join(lost) if lost else f"缺少公式 {missing_math}" if missing_math else "沒有可核對的原文依據" if not ok else "")
        if ok:
            out[idx[k]] = f.model_copy(update={"code": "bridge_checked_by_model" if f.code == "bridge_correctness_unconfirmed"
                                                else "review_withdrawn" if f.origin == "review" else "formula_rewritten",
                                                "severity": "minor", "status": "confirmed",
                                                "detail": f.detail + f"（核對撤回：{(c.guide_quote or c.reason)[:60]}）"})
        else:
            out[idx[k]] = f.model_copy(update={"status": "confirmed", "detail": f.detail + f"（核對未能撤回：{why}）"})
    return out


# -- program validation -------------------------------------------------------------

def _norm_math(s: str) -> str:
    """Cosmetic KaTeX differences only: spacing commands, \\left/\\right, \\dfrac/\\tfrac, braces around one token."""
    t = re.sub(r"\\[,;:!]|\\(?:left|right|displaystyle)\b|\s+", "", s or "")
    t = re.sub(r"\\[dt]frac", r"\\frac", t)
    return re.sub(r"([_^])\{(\\?[A-Za-z0-9])\}", r"\1\2", t)


def source_formulas(text: str) -> tuple[list[str], list[str]]:
    display = re.findall(r"\$\$(.+?)\$\$", text or "", re.S)
    rest = re.sub(r"\$\$.+?\$\$", " ", text or "", flags=re.S)
    inline = re.findall(r"(?<!\$)\$([^$\n]+?)\$(?!\$)", rest)
    return [_norm_math(d) for d in display if d.strip()], [_norm_math(i) for i in inline if i.strip()]


def _is_real_math(f: str) -> bool:
    return any(ch in f for ch in "\\=^_<>+-*/") or len(f) > 3


DEPENDS_ON_CN_RE = re.compile(r"如上所述|前面的對照|上面的中文課文|中文課文(?:已經|中)|對照已經說明|上述譯文")
REDIRECT_RE = re.compile(r"請參考原文|回去看原文|原文有更詳細|詳見原文|請見原文|請對照原文")
PROCESS_RE = re.compile(r"本次修訂|這次修訂|根據你的要求|依照(?:你的|使用者的)?要求|為了回應|依指示|這裡補上剛才|修正後的版本|以下為修正|審稿")


def _check_formulas(src: str, field_text: str, target: str, origin_code: str) -> list[Finding]:
    out = []
    fn = _norm_math(field_text)
    display, inline = source_formulas(src)
    where = "中文課文" if target == "chinese_text" else "主要正文"
    # only a guide that writes math at all can hold an equivalent rewrite worth re-checking
    maybe_rewritten = target == "guide" and "$" in field_text
    code = origin_code if maybe_rewritten else "wrong_formula"
    for f in display:
        if f not in fn:
            out.append(Finding(code=code, severity="major", status="unconfirmed" if maybe_rewritten else "confirmed",
                               target=target, origin="program", source_excerpt=f"$${f}$$",
                               detail=f"{where}沒有原樣保留獨立公式：$${f}$$"))
    for f in inline:
        if f not in fn:
            real = _is_real_math(f)
            out.append(Finding(code=code if real else "formula_paraphrased", severity="major" if real else "minor",
                               status="unconfirmed" if real and maybe_rewritten else "confirmed", target=target,
                               origin="program", source_excerpt=f"${f}$", detail=f"{where}沒有原樣保留行內公式：${f}$"))
    return out


def validate_draft(task: ReadingTaskInput, d: GuideDraft) -> list[Finding]:
    """Program checks, field by field. The 主要正文 must stand on its own: its formulas are looked up in the
    guide only, never in the 中文課文. A formula that is not verbatim (after cosmetic KaTeX normalisation) is an
    *unconfirmed* major finding — an equivalent rewrite can be accepted only through a checked re-check."""
    out: list[Finding] = []
    guide = d.guide or ""
    if len(guide.strip()) < 10:
        out.append(Finding(code="empty_guide", severity="major", detail="主要正文是空的", origin="program"))
    out += _check_formulas(task.text, guide, "guide", "formula_not_verbatim")
    if (guide.count("$") - 2 * guide.count("$$")) % 2 or guide.count("$$") % 2:
        out.append(Finding(code="broken_math", severity="major", detail="正文的 $ 數量不成對，公式會無法顯示", origin="program"))
    for rx, code, what in ((DEPENDS_ON_CN_RE, "depends_on_chinese_text", "主要正文不可依賴中文課文"),
                           (REDIRECT_RE, "redirect_to_source", "主要正文不可要求學生回看原文"),
                           (PROCESS_RE, "process_leak", "主要正文不可敘述產製或修訂過程")):
        m = rx.search(guide)
        if m:
            out.append(Finding(code=code, severity="major", detail=f"{what}：「{m.group(0)}」", origin="program",
                               draft_excerpt=m.group(0)))
    if len(guide) < min(80, len(task.text) * 0.3):
        out.append(Finding(code="short_guide", severity="minor", detail="正文明顯短於原文，可能漏掉內容", origin="program"))
    if not is_chinese(task.text):
        cn = d.chinese_text or ""
        if not cn.strip():
            out.append(Finding(code="missing_chinese_text", severity="major", target="chinese_text", origin="program",
                               detail="原文不是中文，chinese_text 必須是忠實的中文課文"))
        else:
            out += _check_formulas(task.text, cn, "chinese_text", "wrong_formula")
    return out


def decide(findings: list[Finding], *, revised: bool, can_revise: bool, can_verify: bool = False) -> Literal["pass", "revise", "verify", "fail"]:
    """pass: no blocking finding. Confirmed blocking findings are revised once; after that, what a focused re-check
    can settle goes to verify, the rest fails. Only-unconfirmed findings go to verify first (cheaper than a rewrite)."""
    majors = [f for f in findings if f.severity == "major"]
    if not majors:
        return "pass"
    confirmed = [f for f in majors if f.status == "confirmed"]
    verifiable = set(to_verify(findings))
    if not confirmed and can_verify and verifiable:
        return "verify"
    if not revised and can_revise:
        return "revise"
    if can_verify and verifiable and all(findings.index(f) in verifiable for f in majors):
        return "verify"
    return "fail"


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


def understanding_cn_rule(text: str) -> str:
    if is_chinese(text):
        return "這一段原文已經是中文：translations 給空陣列（原文本身就是中文課文）。"
    return ('這一段原文不是中文：translations **必填**——每一句（S 編號）各一項 {"id":"S1","zh":"…"}，把那一句忠實翻成'
            "自然的繁體中文，不增不減，保留條件、例外、否定、量詞與命題力度；公式照抄原文的 LaTeX（反斜線完整保留）。")


def findings_text(fs: list[Finding]) -> str:
    return "\n".join(f"- [{f.code}] {f.detail}" + (f"（原文：{f.source_excerpt}）" if f.source_excerpt else "")
                     + (f"（草稿：{f.draft_excerpt}）" if f.draft_excerpt else "")
                     + (f"→ 修正：{f.suggested_fix}" if f.suggested_fix and f.suggested_fix not in f.detail else "") for f in fs)


def draft_json(d: GuideDraft | dict) -> str:
    return json.dumps(d.model_dump() if isinstance(d, BaseModel) else d, ensure_ascii=False)
