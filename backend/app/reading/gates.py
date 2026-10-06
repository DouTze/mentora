"""Program side of Gate 1 (source understanding) and Gate 2 (主要正文), R3.

Gate 1 checks the *claims list against the full source text*, not the list against itself: the program numbers the
source sentences (S1, S2 …), every claim cites the sentences it rests on (a verbatim sub-quote when it covers only
part of one), every sentence must be cited, and every strength marker of a sentence (negation, condition,
exception, quantifier, modality) must reappear in what the citing claims state. An analysis that leaves out an
exception therefore fails here even when every listed claim is correct. The 中文課文 of a non-Chinese
source keeps every formula verbatim and is audited sentence by sentence by a model whose verdicts must quote it.

Gate 2 turns the body audit into findings with checked evidence: "covered" counts only with a quotation from the
主要正文 (never the 中文課文) that keeps the claim's markers and formulas; a reviewer's "missing" or "distorted"
stands until a revision or a checked re-check; additions must trace to an approved bridge.

Nothing here decides that two sentences mean the same thing; the program decides what evidence is required and
whether the evidence offered is real."""
from __future__ import annotations

import re

from ..tutor.reading import is_chinese
from . import agent as ra
from .contracts import (BodyAudit, BridgePlan, GateCheck, GateResult, SourceAudit, SourceClaim, SourceUnderstanding)
from .markers import parity, profile, CATEGORIES, EN, ZH, ZH_NEG_RE
from .source import sentence_cuts

LIST_MARK_RE = re.compile(r"\s*(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)、]\s*|\s*\(\w{1,3}\)\s*|\s*[-*+]\s*")
SEGMENT_COVERAGE = 0.85
SUPPLEMENT_MATCH = 0.3         # share of a 補充說明 paragraph's character bigrams found in some approved bridge        # share of a sentence's characters that claim anchors must cover
MATH_HINT_RE = re.compile(r"[∑∫√≤≥≠≈∞∂∇αβγδεθλμσπρφωΩΣΔΠ×÷±→⇒∈∉⊂⊆∀∃]|\b[a-zA-Z]\s*[=<>]\s*[\w(]|\^|_\{?\w")
# a bridge narrating how the handout was made or revised ("本次修訂…", "依照你的要求…"). Only such phrases: the bare
# words 使用者 / 這次 / 修訂 are subject matter too (NLU 判斷使用者意圖, 法規修訂), and catching them failed real bridges.
PRODUCTION_RE = re.compile(r"(?:本次|這次)(?:修訂|修改|改寫|補充)|修訂(?:後的|版本|過程)|(?:根據|依照|依)(?:你|使用者)?的?(?:要求|指示)"
                           r"|(?:你|使用者)的(?:要求|指示)|為了回應|審稿|講義中加入|這裡補上剛才|以下為修正")


# -- source segments ------------------------------------------------------------------------------

def numbered_source(text: str) -> str:
    """The source as the model sees it in the source stage: every sentence with its program-assigned id, paragraphs
    separated by blank lines (the full text, nothing left out)."""
    lines, sid = [], 0
    for block in re.split(r"\n\s*\n", text or ""):
        segs = source_segments(block)
        if not segs:
            continue
        lines.append("\n".join(f"［S{sid + k}］{s}" for k, (_, s) in enumerate(segs, 1)))
        sid += len(segs)
    return "\n\n".join(lines)


def source_segments(text: str) -> list[tuple[str, str]]:
    """[(S1, sentence), …] in reading order: blocks (blank-line separated) cut at sentence ends outside math;
    a display formula is its own segment. Segments with no letters / digits are skipped."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if not block:
            continue
        if block.startswith("$$") and block.endswith("$$"):
            out.append(block)
            continue
        cuts = [0] + sentence_cuts(block) + [len(block)]
        carry = ""
        for a, b in zip(cuts, cuts[1:]):
            seg = (carry + block[a:b]).strip() if carry else block[a:b].strip()
            carry = ""
            if LIST_MARK_RE.fullmatch(seg):   # "1." / "(a)" is a list marker, not a sentence: it opens the next one
                carry = block[a:b]
                continue
            if seg and re.search(r"\w", seg):
                out.append(seg)
        if carry.strip():
            out.append(carry.strip())
    return [(f"S{i}", s) for i, s in enumerate(out, 1)]


def _span(hay_norm: str, quote: str, start: int = 0) -> tuple[int, int] | None:
    q = ra.norm_text(quote).strip("….。，,;；:：「」\"'")
    if len(q) < 2:
        return None
    i = hay_norm.find(q, start)
    if i < 0 and start:
        i = hay_norm.find(q)
    return (i, i + len(q)) if i >= 0 else None


def _norm_index(seg: str) -> list[int]:
    """Original offset → offset in ra.norm_text(seg) (the same characters ra.norm_text drops are skipped)."""
    import unicodedata
    out, n = [], 0
    for ch in seg:
        out.append(n)
        n += len(re.sub(r"[\s*_`>]+", "", unicodedata.normalize("NFKC", ch)))
    return out


def _marker_positions(seg: str) -> list[tuple[int, str]]:
    """(offset in the normalized segment, marker) of every strength marker, found on the original text (English
    word boundaries need the spaces) and mapped to the whitespace-free form used for coverage."""
    from .markers import ZH_NEG_EXEMPT
    idx = _norm_index(seg)
    exempt = {i for w in ZH_NEG_EXEMPT for m in re.finditer(re.escape(w), seg) for i in range(m.start(), m.end())}
    hits = [(m.start(), m.group(0)) for m in ZH_NEG_RE.finditer(seg) if m.start() not in exempt]
    for rx in list(ZH.values()) + list(EN.values()):
        hits += [(m.start(), m.group(0)) for m in rx.finditer(seg)]
    math = [(m.start(), m.end()) for m in re.finditer(r"```.*?(?:```|$)|\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`", seg, re.S)]
    return [(idx[i], w) for i, w in hits if not any(a <= i < b for a, b in math)]


# -- Gate 1: program checks ------------------------------------------------------------------------

def assemble_chinese_text(source: str, u: SourceUnderstanding) -> str | None:
    """The 中文課文 of a non-Chinese source, built by the program from the per-sentence translations in source order
    (paragraph breaks kept); None for a Chinese source (the source is its own 中文課文)."""
    if is_chinese(source):
        return None
    tr = {t.id: t.zh.strip() for t in u.translations}
    paras, sid = [], 0
    for block in re.split(r"\n\s*\n", source or ""):
        segs = source_segments(block)
        if segs:
            paras.append("".join(tr.get(f"S{sid + k}", "") for k in range(1, len(segs) + 1)))
            sid += len(segs)
    text = "\n\n".join(p for p in paras if p)
    return text or None


def with_chinese_text(source: str, u: SourceUnderstanding) -> SourceUnderstanding:
    return u.model_copy(update={"chinese_text": assemble_chinese_text(source, u)})


def resolve_claims(source: str, claims: list[SourceClaim], translations: list | None = None) -> list[SourceClaim]:
    """Set each claim's source_text from what it cites (its quote, else the cited sentences) and fill an empty
    statement with what it restates: the cited Chinese text, or the cited sentences' translations."""
    segs = dict(source_segments(source))
    tr = {t.id: t.zh for t in translations or []}
    out = []
    for c in claims:
        src = c.quote.strip() or " ".join(segs[x] for x in c.sentences if x in segs)
        stmt = c.statement.strip() or (src if is_chinese(source) else "".join(tr.get(x, "") for x in c.sentences)) or src
        out.append(c.model_copy(update={"source_text": src, "statement": stmt}))
    return out


def gate1_program(source: str, u: SourceUnderstanding, *, source_kind: str = "md",
                  symbol_evidence: dict | None = None) -> list[GateCheck]:
    checks: list[GateCheck] = []
    segments = source_segments(source)
    segs = dict(segments)
    claims = resolve_claims(source, u.claims, u.translations)
    raw = {c.id: c for c in u.claims}
    seen_ids: set[str] = set()
    citing: dict[str, list[SourceClaim]] = {sid: [] for sid, _ in segments}
    for c in claims:
        bad = [x for x in c.sentences if x not in segs]
        problem = ("編號重複" if c.id in seen_ids else f"引用了不存在的句子 {bad}" if bad else
                   "quote 不是所引句子中的一段（必須逐字複製）" if c.quote.strip() and not any(
                       ra.contains_quote(segs[x], c.quote) for x in c.sentences) else "")
        seen_ids.add(c.id)
        checks.append(GateCheck(id=f"anchor:{c.id}", kind="anchor", status="fail" if problem else "pass",
                                detail=f"{c.id} {problem}" if problem else "", evidence="、".join(c.sentences)))
        if problem:
            continue
        for x in c.sentences:
            citing[x].append(c)
        src = c.quote.strip() or " ".join(segs[x] for x in c.sentences)
        why = rewritten(source, raw.get(c.id, c), c, u.translations, {ra._norm_math(f) for f in evidence_formulas(symbol_evidence)})
        if why:   # a sentence being cited proves where the claim comes from, not that it is understood correctly
            checks.append(GateCheck(id=f"rewrite:{c.id}", kind="rewrite_check", status="fail", evidence=c.statement[:80],
                                    detail=f"{c.id} {why}，必須經語意核對才能成為已驗證命題"))
        added = [m for m in parity(src, _stated(c)) if "多了否定" in m]
        if added:
            checks.append(GateCheck(id=f"strength:{c.id}", kind="strength_flag", status="fail", evidence=c.statement[:80],
                                    detail=f"{c.id} 的命題可能與原文力度不同：{'；'.join(added)}"))
    # full-text coverage: every sentence cited; a sentence cited only by partial quotes must be (almost) all quoted;
    # every strength marker of a sentence must reappear in what its claims state
    for sid, seg in segments:
        cs = citing[sid]
        if not cs:
            checks.append(GateCheck(id=f"coverage:{sid}", kind="coverage", status="fail", evidence=seg[:80],
                                    detail=f"原文 {sid} 沒有任何命題涵蓋：{seg[:80]}"))
            continue
        if all(c.quote.strip() for c in cs):
            sn = ra.norm_text(seg)
            mask = [False] * len(sn)
            for c in cs:
                sp = _span(sn, c.quote)
                if sp:
                    for k in range(*sp):
                        mask[k] = True
            share = sum(mask) / max(1, len(mask))
            loose = sorted({w for off, w in _marker_positions(seg) if off < len(mask) and not mask[off]})
            if share < SEGMENT_COVERAGE or loose:
                checks.append(GateCheck(id=f"coverage:{sid}", kind="coverage", status="fail", evidence=seg[:80],
                                        detail=f"原文 {sid} 只被部分涵蓋（{share:.0%}）" +
                                               (f"，未涵蓋：{'、'.join(loose)}" if loose else "") + f"：{seg[:80]}"))
                continue
        checks.append(GateCheck(id=f"coverage:{sid}", kind="coverage", status="pass", evidence=seg[:60]))
        lost = [m for m in parity(seg, " ".join(_stated(c) for c in cs)) if "多了否定" not in m]
        if lost:
            checks.append(GateCheck(id=f"strength:{sid}", kind="strength_flag", status="fail", evidence=seg[:80],
                                    detail=f"原文 {sid} 的命題力度可能沒有保留（{'、'.join(c.id for c in cs)}）：{'；'.join(lost)}"))
    if not is_chinese(source):   # the 中文課文: one translation per sentence, formulas verbatim, markers kept
        tr = {t.id: t.zh for t in u.translations}
        for x in sorted(set(tr) - set(segs)):
            checks.append(GateCheck(id=f"translation:{x}", kind="translation", status="fail",
                                    detail=f"翻譯了不存在的句子 {x}"))
        for sid, seg in segments:
            zh = tr.get(sid, "").strip()
            if not zh:
                checks.append(GateCheck(id=f"translation:{sid}", kind="translation", status="fail", evidence=seg[:80],
                                        detail=f"{sid} 沒有中文課文：{seg[:60]}"))
                continue
            dm, im = ra.source_formulas(seg)
            zn = ra._norm_math(zh)
            miss = [f for f in dm + [i for i in im if ra._is_real_math(i)] if f not in zn]
            if miss:
                checks.append(GateCheck(id=f"translation:{sid}", kind="translation", status="fail", evidence=zh[:80],
                                        detail=f"{sid} 的中文課文沒有原樣保留公式 {miss}"))
                continue
            lost = parity(seg, zh)
            checks.append(GateCheck(id=f"translation:{sid}", kind="translation_flag" if lost else "translation",
                                    status="fail" if lost else "pass", evidence=zh[:80],
                                    detail=f"{sid} 的中文課文可能沒有保留命題力度：{'；'.join(lost)}" if lost else ""))
    # formulas read from the PDF regions are required content: the claims (or the 中文課文) must carry each one
    stated_math = ra._norm_math(" ".join([u.chinese_text or ""] + [" ".join(c.formulas) + " " + c.statement for c in claims]))
    for k, f in enumerate(evidence_formulas(symbol_evidence), 1):
        ok = ra._norm_math(f) in stated_math
        checks.append(GateCheck(id=f"symbol_formula:V{k}", kind="symbol_formula", status="pass" if ok else "fail",
                                evidence=f, detail="" if ok else f"依 PDF 版面取證的公式 ${f}$ 沒有出現在命題中，請在相關命題的 formulas 寫出"))
    note = notation_check(source, source_kind, symbol_evidence)
    if note.status == "pass" and (symbol_evidence or {}).get("status") == "confirmed":
        mism = notation_mismatches(symbol_evidence, [u.chinese_text or ""] + [" ".join(c.formulas) + " " + c.statement
                                                                              for c in claims])
        if mism:
            note = note.model_copy(update={"status": "fail", "detail": "；".join(mism)})
    checks.append(note)
    return checks


def evidence_formulas(evidence: dict | None) -> list[str]:
    """Formulas read from the rendered PDF regions (confirmed symbol evidence); each one must reach the claims and
    the 主要正文, since the text layer lost them."""
    ev = evidence or {}
    if ev.get("status") != "confirmed":
        return []
    return [f.strip() for f in (ev.get("transcription") or {}).get("formulas", []) if f.strip()]


CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
# spans compared character for character: fenced and inline code, math, string literals (ASCII, curly and corner
# quotes) and indented code lines; whitespace in them can be meaning ("a  b" has length 4, "a b" 3)
VERBATIM_RE = re.compile(r"```.*?(?:```|$)|`[^`\n]*`|\$\$.*?\$\$|\$[^$\n]*\$|\"[^\"\n]*\"|'[^'\n]*'|“[^”\n]*”|‘[^’\n]*’|"
                         r"「[^」\n]*」|『[^』\n]*』|^(?: {4}|\t).*$", re.S | re.M)


def _prose_ws(t: str) -> str:
    t = re.sub(r"\s+", " ", t)
    return re.sub(rf"(?<=[{CJK}]) | (?=[{CJK}])", "", t)


def _plain(t: str) -> str:
    """The comparison key for "is this the same text". Only differences that provably never change meaning are
    removed, and only in prose: whitespace runs (a space next to a CJK character carries nothing; elsewhere one
    space stays) and sentence-final punctuation at the very end. Code, string literals, math and indented lines are
    kept character for character (VERBATIM_RE); digits, decimal points, signs, symbols and every inner punctuation
    mark are kept everywhere (".5" ≠ "5", "2**3" ≠ "23", "不，是" ≠ "不是"). Any other difference means the text
    is not the same and goes to a model check."""
    t = t or ""
    out, last = [], 0
    for m in VERBATIM_RE.finditer(t):
        out.append(_prose_ws(t[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    rest = _prose_ws(t[last:])
    out.append(rest)
    out[0] = out[0].lstrip()                 # only prose ends are trimmed; a verbatim span keeps its spacing
    if rest.strip() or len(out) == 1:
        out[-1] = out[-1].rstrip().rstrip("。．.！!？?；;").rstrip()
    else:
        out[-1] = ""
    return "".join(out)


def rewritten(source: str, raw: SourceClaim, resolved: SourceClaim, translations: list | None,
              evidence_math: set[str] | None = None) -> str:
    """Why a claim is more than the cited text itself ("" = the fast path: it restates the cited source, or for a
    non-Chinese source the cited sentences' audited translation, and adds nothing)."""
    reasons = []
    if raw.statement.strip():
        tr = {t.id: t.zh for t in translations or []}
        default = resolved.source_text if is_chinese(source) else "".join(tr.get(x, "") for x in raw.sentences)
        if _plain(raw.statement) != _plain(default) and _plain(raw.statement) != _plain(resolved.source_text):
            reasons.append("的陳述經過改寫")
    if raw.conditions or raw.exceptions or raw.strength.strip():
        reasons.append("附加了條件／例外／力度欄位")
    src_math = ra._norm_math(resolved.source_text)
    if any(ra._norm_math(f) not in src_math and ra._norm_math(f) not in (evidence_math or set())
           for f in raw.formulas if f.strip()):
        reasons.append("列出所引原文沒有逐字出現的公式")
    return "、".join(reasons)


def _stated(c: SourceClaim) -> str:
    return " ".join([c.statement, *c.conditions, *c.exceptions, c.strength, *c.formulas])


BOLD_WRAP_RE = r"\\(?:mathbf|boldsymbol|bm|mathbfit|pmb)\s*\{\s*%s\s*\}"


def transcription_against_clues(tr, clues: dict) -> list[str]:
    """Contradictions between a vision transcription and the font clues of the same regions (empty = consistent).
    Clues never decide the notation; they can only make a reading suspicious."""
    problems = []
    if tr.unreadable:
        problems.append("模型表示部分符號無法辨識：" + "、".join(tr.unreadable[:4]))
    text = " ".join(tr.formulas + [x.latex for x in tr.symbols])
    bold_seen = {x.symbol for x in tr.symbols if x.style == "bold"}
    for t in clues.get("bold_tokens") or []:
        if len(t) == 1 and t.isalpha() and t not in bold_seen and not re.search(BOLD_WRAP_RE % re.escape(t), text):
            problems.append(f"字型顯示 {t} 為粗體，但轉寫沒有標成粗體")
    for t in clues.get("italic_tokens") or []:
        if len(t) == 1 and t.isalpha() and any(x.symbol == t and x.style == "bold" for x in tr.symbols):
            problems.append(f"字型顯示 {t} 為斜體，但轉寫標成粗體")
    for t in clues.get("superscripts") or []:
        if t.strip() and not re.search(r"\^\s*\{?\s*" + re.escape(t.strip()), text):
            problems.append(f"字型顯示 {t} 是上標，但轉寫沒有寫成上標")
    return problems


def notation_mismatches(evidence: dict, texts: list[str]) -> list[str]:
    """Where the understanding writes a symbol the evidence shows in bold as a plain letter inside math."""
    bold = {x["symbol"] for x in (evidence.get("transcription") or {}).get("symbols", []) if x.get("style") == "bold"}
    math = " ".join(m for t in texts for m in re.findall(r"\$\$.+?\$\$|\$[^$\n]+\$", t or "", re.S))
    out = []
    for sym in sorted(bold):
        if len(sym) != 1:
            continue
        stripped = re.sub(BOLD_WRAP_RE % re.escape(sym), " ", math)
        if re.search(r"(?<![\\A-Za-z])" + re.escape(sym) + r"(?![A-Za-z])", stripped):
            out.append(f"依原書版面，{sym} 是粗體（寫成 \\mathbf{{{sym}}}），中文課文或命題的公式寫成了一般字母")
    return out


def notation_check(source: str, source_kind: str, evidence: dict | None) -> GateCheck:
    """Symbols are verified from source evidence, never from the extracted text alone (WaAi-ML notation rules):
    - no formula or symbol → not_applicable (with the reason);
    - md / txt: the LaTeX the author wrote is the evidence; verbatim preservation is checked by the formula items;
    - pdf: the text layer loses bold / italic / sub- and superscripts, so symbols stay `unverified` unless symbol
      evidence for this unit was confirmed (R5); a revision cannot fix missing evidence."""
    has_math = bool(re.search(r"\$", source)) or bool(MATH_HINT_RE.search(source))
    if not has_math and not (evidence or {}).get("status"):
        return GateCheck(id="notation", kind="notation", status="not_applicable", detail="這一段沒有公式或數學符號")
    if source_kind != "pdf":
        return GateCheck(id="notation", kind="notation", status="pass", detail="原文以 LaTeX／純文字寫出符號；逐字保留由公式項目檢查",
                         evidence="source text")
    ev = evidence or {}
    if source_kind == "pdf" and not has_math and not ev:
        return GateCheck(id="notation", kind="notation", status="not_applicable", detail="這一段沒有公式或數學符號")
    if ev.get("status") == "confirmed":
        return GateCheck(id="notation", kind="notation", status="pass", detail="PDF 符號已有視覺取證",
                         evidence=ev.get("summary", ""))
    return GateCheck(id="notation", kind="notation", status="unverified", fixable=False,
                     detail=ev.get("reason") or "PDF 文字層無法確認符號的字型（粗體／斜體／上下標），目前沒有可用的視覺取證",
                     evidence=ev.get("summary", ""))


# Version of the Gate 1 / Gate 2 acceptance rules. Anything that passed the gates is reused only under the same
# rules: the VerifiedSource cache (key and stored entry), a prefetched guide and the durable per-node results of a
# guide operation all carry it, so work accepted by older (looser) rules is checked again. The label is bumped on
# purpose; the hash of the modules that decide acceptance also changes it whenever that code changes, so a
# forgotten bump cannot reuse a stale verification.
GATE_RULES_LABEL = "gate1-v3"


def _verifier_version() -> str:
    import hashlib
    from pathlib import Path
    here = Path(__file__).parent
    h = hashlib.sha256()
    for name in ("gates.py", "markers.py", "contracts.py", "agent.py"):
        h.update((here / name).read_bytes())
    return f"{GATE_RULES_LABEL}+{h.hexdigest()[:12]}"


GATE_RULES_VERSION = _verifier_version()

CLAIM_FLAGS = ("strength_flag", "rewrite_check")


def needs_source_audit(source: str, checks: list[GateCheck]) -> bool:
    """A non-Chinese source always needs the translation audit; a Chinese one when a claim was rewritten or carries
    added fields, or a strength marker looks lost (the cited text alone is the fast path)."""
    if not is_chinese(source):
        return True
    return any(c.kind in CLAIM_FLAGS and c.status == "fail" for c in checks)


def flagged_claims(checks: list[GateCheck], claims: list[SourceClaim]) -> set[str]:
    """Claim ids behind the strength / rewrite flags (a sentence flag points at every claim citing that sentence)."""
    out: set[str] = set()
    for c in checks:
        if c.kind in CLAIM_FLAGS and c.status == "fail":
            ref = c.id.split(":", 1)[1]
            out |= {x.id for x in claims if x.id == ref or ref in x.sentences}
    return out


def audit_items(source: str, u: SourceUnderstanding, checks: list[GateCheck]) -> tuple[list[tuple[str, str]], list[SourceClaim]]:
    """(sentences to audit, claims to audit): every sentence of a non-Chinese source (translation); claims behind a
    strength or rewrite flag, in any language."""
    flagged = flagged_claims(checks, u.claims)
    claims = [c for c in resolve_claims(source, u.claims, u.translations) if c.id in flagged]
    return (source_segments(source) if not is_chinese(source) else []), claims


def audit_check(sentences: list[tuple[str, str]], claims: list[SourceClaim]):
    from ..llm.tasks import OutputInvalid
    want_s, want_c = [s for s, _ in sentences], [c.id for c in claims]

    def check(a: SourceAudit) -> None:
        for name, want, got in (("sentences", want_s, [x.id for x in a.sentences]), ("claims", want_c, [x.id for x in a.claims])):
            missing = [w for w in want if w not in got]
            dup = sorted({g for g in got if got.count(g) > 1})
            if missing or dup:
                raise OutputInvalid(f"{name} 必須對每一項各回報一次：{'、'.join(want) or '（無）'}"
                                    + (f"；缺少 {missing}" if missing else "") + (f"；重複 {dup}" if dup else ""))
    return check


def gate1_apply_audit(source: str, u: SourceUnderstanding, checks: list[GateCheck], audit: SourceAudit,
                      sentences: list[tuple[str, str]]) -> list[GateCheck]:
    """Merge the audit into the Gate 1 checks. A "faithful" sentence verdict counts only with a quotation from the
    中文課文 that keeps the sentence's markers and formulas; a strength flag is settled only when every claim behind
    it was judged faithful."""
    out = list(checks)
    text = dict(sentences)
    for s_ in audit.sentences:
        if s_.id not in text:
            continue
        k = next((i for i, c in enumerate(out) if c.id == f"translation:{s_.id}"), None)
        if s_.verdict == "faithful":
            if k is not None and out[k].kind == "translation_flag":   # a marker heuristic settled by the audit
                out[k] = out[k].model_copy(update={"status": "pass", "detail": out[k].detail + "（核對：力度一致）"})
            continue
        what = {"missing": "漏譯", "distorted": "改變原意", "unsure": "無法確認"}[s_.verdict]
        bad = GateCheck(id=f"translation:{s_.id}", kind="translation", status="fail", evidence=text[s_.id][:80],
                        detail=f"{s_.id} 的中文課文{what}：{s_.note or text[s_.id][:60]}")
        if k is None:
            out.append(bad)
        else:
            out[k] = bad
    verdict = {c.id: c for c in audit.claims}
    for k, c in enumerate(out):
        if c.kind in CLAIM_FLAGS and c.status == "fail":
            behind = flagged_claims([c], u.claims)
            if behind and all(verdict.get(x) and verdict[x].verdict == "faithful" for x in behind):
                out[k] = c.model_copy(update={"status": "pass", "detail": c.detail + "（核對：力度一致）"})
    for cid, v in verdict.items():
        if v.verdict != "faithful":
            out.append(GateCheck(id=f"claim_audit:{cid}", kind="claim_audit", status="fail",
                                 detail=f"{cid} 的命題與原文不一致：{v.note[:80]}"))
    return out


def understanding_schema(source: str) -> dict:
    """Constrained-decoding schema of the source stage: sentence ids are an enum, claims are capped at a little more
    than one per sentence (a small model otherwise keeps appending redundant combined claims until the output is
    cut), and a non-Chinese source has exactly one translation per sentence (a Chinese one none)."""
    from .contracts import SourceUnderstanding
    ids = [sid for sid, _ in source_segments(source)] or ["S1"]
    sch = SourceUnderstanding.model_json_schema()
    defs = sch.get("$defs", {})
    claim = dict(defs["SourceClaim"])
    claim["properties"] = {**claim["properties"], "sentences": {"type": "array", "items": {"type": "string", "enum": ids},
                                                                "minItems": 1, "maxItems": min(len(ids), 4)}}
    sch["properties"]["claims"] = {"type": "array", "items": claim, "minItems": 1, "maxItems": len(ids) + 3}
    tr = dict(defs["SentenceTranslation"])
    tr["properties"] = {**tr["properties"], "id": {"type": "string", "enum": ids}}
    n = 0 if is_chinese(source) else len(ids)
    sch["properties"]["translations"] = {"type": "array", "items": tr, "minItems": n, "maxItems": n}
    sch["required"] = sorted(set(sch.get("required", [])) | {"claims", "translations"})
    return sch


def body_tokens(source: str, bridges: int = 0) -> int:
    """Output reserved for the 主要正文: about twice the source plus room per bridge, 2048–4096 tokens."""
    from ..llm.context import estimate_tokens
    return max(2048, min(4096, 2 * estimate_tokens(source) + 300 * bridges + 512))


def body_schema(source: str, bridges: int = 0) -> dict:
    """Constrained-decoding schema of the 主要正文 with bounded strings. A small model sometimes falls into repeating
    "\\n" until the token limit, leaving JSON that cannot be parsed; with a length bound the string is closed and the
    (useless) guide is caught by the gates and revised instead of wasting a format fix."""
    from .contracts import BodyDraft
    sch = BodyDraft.model_json_schema()
    props = dict(sch["properties"])
    props["guide"] = {**props["guide"], "maxLength": int(body_tokens(source, bridges) * 0.8)}
    props["key_points"] = {"type": "array", "items": {"type": "string", "maxLength": 160}, "maxItems": 3}
    props["check"] = {**props["check"], "maxLength": 240}
    props["limits"] = {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 4}
    sch["properties"] = props
    return sch


def exact_ids_schema(model_cls, lists: dict[str, list[str]]) -> dict:
    """The model's JSON schema with each listed array restricted to exactly these ids (enum + item count), so
    constrained decoding cannot leave an expected check out. The Pydantic check still validates the result."""
    sch = model_cls.model_json_schema()
    defs = sch.get("$defs", {})
    for name, ids in lists.items():
        prop = sch["properties"][name]
        ref = prop["items"].get("$ref", "").split("/")[-1]
        item = dict(defs[ref]) if ref else dict(prop["items"])
        item["properties"] = {**item["properties"], "id": {"type": "string", "enum": ids or ["-"]}}
        sch["properties"][name] = {"type": "array", "items": item, "minItems": len(ids), "maxItems": len(ids)}
    return bound_strings(sch)


FREE_TEXT_MAX = 400   # chars of one free-text field of a check / audit answer: a reason, a note, a quoted fragment


def bound_strings(sch: dict, limit: int = FREE_TEXT_MAX) -> dict:
    """Every free-text string of a check / audit schema gets a maxLength (in place). A small model sometimes repeats
    one clause inside a reason until the token limit, leaving JSON that cannot be parsed; bounded, the string closes
    and the answer still parses. Quoted fragments are located by prefix, so a cut quote still points at its text."""
    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and not {"enum", "const", "maxLength"} & node.keys():
                node["maxLength"] = limit
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(sch)
    return sch


VERIFY_QUOTE_MAX, VERIFY_REASON_MAX = 200, 300


def verify_schema(n_items: int) -> dict:
    """verify output: at most one check per listed item, numbered 1..n, with bounded free text (verify_tokens
    leaves room for every field at its bound, so the answer is never cut)."""
    from .agent import VerifyResult
    sch = VerifyResult.model_json_schema()
    check = sch["$defs"]["VerifyCheck"]
    props = check["properties"]
    check["properties"] = {**props, "item": {"type": "integer", "enum": list(range(1, n_items + 1))},
                           "guide_quote": {**props["guide_quote"], "maxLength": VERIFY_QUOTE_MAX},
                           "reason": {**props["reason"], "maxLength": VERIFY_REASON_MAX}}
    sch["properties"]["checks"] = {**sch["properties"]["checks"], "maxItems": n_items}
    return bound_strings(sch)


def verify_tokens(n_items: int) -> int:
    """Output budget of verify: every check at its bounds (about a token per Chinese char) plus the JSON around."""
    return min(4096, max(512 + 320 * n_items, 128 + n_items * (VERIFY_QUOTE_MAX + VERIFY_REASON_MAX + 80)))


def gate1_decide(checks: list[GateCheck], *, audited: bool, need_audit: bool, can_audit: bool,
                 can_revise: bool) -> str:
    """pass | audit | revise | hold.
    - structural failures (unanchored claims, uncovered sentences, missing formulas or 中文課文, audit failures)
      are revised — before an audit is spent on them;
    - strength flags alone go to the audit (a focused model check may settle a marker heuristic);
    - a source that needs the audit is not passed without it;
    - missing symbol evidence (unverified) holds: no revision can supply it."""
    fails = [c for c in checks if c.status == "fail"]
    unverified = [c for c in checks if c.status == "unverified"]
    structural = [c for c in fails if c.kind not in ("strength_flag", "translation_flag", "rewrite_check")]
    if any(not c.fixable for c in unverified):   # missing evidence: nothing a revision or an audit can supply
        return "hold"
    if structural:
        return "revise" if can_revise else "hold"
    if fails:   # claim flags only (strength / rewrite)
        if need_audit and not audited and can_audit:
            return "audit"
        return "revise" if can_revise else "hold"
    if unverified:
        return "hold"
    if need_audit and not audited:
        return "audit" if can_audit else "hold"
    return "pass"


# -- Gate 2 ------------------------------------------------------------------------------------------

def bridge_check(claim_ids: set[str], learning_has_reading: bool):
    from ..llm.tasks import OutputInvalid

    def check(p: BridgePlan) -> None:
        errs = []
        ids = [b.id for b in p.bridges]
        if len(ids) != len(set(ids)):
            errs.append("橋梁編號重複")
        if len(ids) > 4:
            errs.append("最多 4 個橋梁；只保留移除後會妨礙理解的")
        for b in p.bridges:
            bad = [t for t in b.targets if t not in claim_ids]
            if bad:
                errs.append(f"{b.id} 的 targets 只能用這些命題編號：{'、'.join(sorted(claim_ids))}（不存在 {bad}）")
            if PRODUCTION_RE.search(b.content):
                errs.append(f"{b.id} 的 content 只能寫學科內容，不可寫產製或修訂過程")
            if b.basis == "prior_reading" and not learning_has_reading:
                errs.append(f"{b.id} 標為 prior_reading，但讀者還沒有讀過前面的段落")
        if errs:
            raise OutputInvalid("；".join(errs))
    return check


def body_audit_check(claims: list[SourceClaim], bridges: BridgePlan):
    from ..llm.tasks import OutputInvalid
    want_c, want_b = [c.id for c in claims], [b.id for b in bridges.bridges]

    def check(a: BodyAudit) -> None:
        for name, want, got in (("claims", want_c, [x.id for x in a.claims]), ("bridges", want_b, [x.id for x in a.bridges])):
            missing = [w for w in want if w not in got]
            extra = sorted({g for g in got if g not in want})
            dup = sorted({g for g in got if got.count(g) > 1})
            if missing or extra or dup:
                raise OutputInvalid(f"{name} 必須對每一項各回報一次：{'、'.join(want) or '（無，給空陣列）'}"
                                    + (f"；缺少 {missing}" if missing else "") + (f"；不存在 {extra}" if extra else "")
                                    + (f"；重複 {dup}" if dup else ""))
    return check


def ra_overlap(text: str, ref: str) -> float:
    """Share of `text`'s character bigrams that occur in `ref` (whitespace / punctuation ignored)."""
    def bi(t):
        t = re.sub(r"[\s，。、；：！？,.;:!?「」（）()*`$]+", "", t or "")
        return {t[i:i + 2] for i in range(len(t) - 1)}
    a = bi(text)
    return len(a & bi(ref)) / len(a) if a else 1.0


CLAUSE_RE = re.compile(r"[，,；;：:。！？!?\n]")
OTHER_SENTENCE_MATCH = 0.5     # share of a guide clause's bigrams found in another source sentence: it restates that one


def coverage_parity(claim_src: str, full: str, source: str) -> list[str]:
    """parity() of a claim against the guide sentence(s) quoted as covering it. A lost marker counts anywhere in
    the sentence. An added negation counts only in the clauses that do not restate another source sentence: a long
    guide sentence often carries the next claims too ("ELIZA…，但並不具備語意理解"), and their 並不 / 無法 are
    checked against their own sentences. A clause like "這並不正確" restates nothing, so it still counts here."""
    lost = [m for m in parity(claim_src, full) if "多了否定" not in m]
    # the claim's own sentence(s) — also the one a partial quote ("彼此獨立") comes from — are never "another"
    others = [s for _, s in source_segments(source)
              if ra.norm_text(s) not in ra.norm_text(claim_src) and ra_overlap(claim_src, s) < OTHER_SENTENCE_MATCH]
    mine = []
    for clause in CLAUSE_RE.split(full or ""):
        if not clause.strip():
            continue
        best = max((ra_overlap(clause, s) for s in others), default=0.0)
        if best < OTHER_SENTENCE_MATCH or best <= ra_overlap(clause, claim_src):
            mine.append(clause)
    added = [m for m in parity(claim_src, "，".join(mine)) if "多了否定" in m]
    return lost + added


def restated(c: SourceClaim, source: str) -> str:
    """What the guide is compared with for a claim: the source text of a Chinese source; for any other source, the
    claim's audited Chinese statement (Gate 1 checked it against the source). An English sentence shares no wording
    with a Chinese guide, and its markers ("not just … but also", "can be taken beyond") have no word-for-word
    counterpart, so comparing across languages fails faithful guides."""
    return c.source_text if is_chinese(source) else c.statement


def restated_source(claims: list[SourceClaim], source: str) -> str:
    """The source in the language of the guide: itself, or the claims' statements, one per paragraph."""
    return source if is_chinese(source) else "\n\n".join(c.statement for c in claims)


ABSENT_BELOW = 0.5  # a claim whose closest guide passage shares less of its wording is not in the guide
# a list lead-in ("• 代表技術與模型：", "Examples:"): it says nothing on its own; the items after it carry the content
LEAD_IN_RE = re.compile(r"\W*[^\W][^。.!?！？]{0,15}[：:]\s*")


def best_match(text: str, guide: str) -> tuple[float, str]:
    """(share of `text`'s wording found, passage) for the guide passage — one sentence or two adjacent ones — that
    carries most of it. Located by the program, so an auditor quoting the wrong sentence cannot hide a passage
    that is there, nor invent one that is not. Bullets and other non-word marks are ignored."""
    def bare(t):
        return re.sub(r"[^\w]", "", t or "")
    ss = [s for _, s in source_segments(guide)]
    best = max(((ra_overlap(bare(text), bare(s)), s) for s in ss), default=(0.0, ""), key=lambda x: x[0])
    for a, b in zip(ss, ss[1:]):   # two sentences only when the claim clearly spans them (a pair never scores lower)
        share = ra_overlap(bare(text), bare(a + b))
        if share > best[0] + 0.1:
            best = (share, a + " " + b)
    return best


def unconfirmed_coverage(c: SourceClaim, guide: str, source: str, basis: str, why: str, quote: str = "", *,
                         formulas_missing: bool = False) -> ra.Finding:
    """The auditor said "covered" (or "unsure") without evidence the program accepts. The program looks for the
    claim itself instead of failing on the missing evidence:
    - nothing in the guide carries its wording → missing (a program fact; revised, then held);
    - the closest passage lost a strength marker / added a negation, or a formula is missing → stays a blocking
      question for the re-check;
    - otherwise it is there as far as a program can tell → a warning shown with the guide, no rewrite (rewrites on
      no evidence deleted faithful passages).
    `basis`: the source in the guide's language (restated_source), for telling this claim's clauses from others'."""
    text = restated(c, source)
    share, where = best_match(text, guide)
    head = f"{c.id}「{c.statement[:50]}」"
    if share < ABSENT_BELOW and LEAD_IN_RE.fullmatch(c.source_text.strip()):
        return ra.Finding(code="coverage_unconfirmed", severity="minor", status="unconfirmed", origin="review",
                          item=c.id, source_excerpt=c.source_text, detail=f"{head}是列表的引導標題，正文沒有照寫")
    if share < ABSENT_BELOW:
        return ra.Finding(code="missing_key_claim", severity="major", origin="program", item=c.id,
                          source_excerpt=c.source_text, suggested_fix=c.statement,
                          detail=f"{head}沒有寫進正文（最接近的句子只有 {share:.0%} 的字詞相同）")
    lost = coverage_parity(text, where, basis)
    if lost or formulas_missing:
        return ra.Finding(code="coverage_unconfirmed", severity="major", status="unconfirmed", origin="review",
                          item=c.id, source_excerpt=c.source_text, draft_excerpt=quote, detail=head + why)
    return ra.Finding(code="coverage_unconfirmed", severity="minor", status="unconfirmed", origin="review", item=c.id,
                      source_excerpt=c.source_text, draft_excerpt=where,
                      detail=f"{head}{why}；程式找到的對應句子：{where[:60]}")


def reader_notes(findings: list[ra.Finding]) -> list[dict]:
    """What the published guide tells the reader it could not check: source content whose restatement the program
    found in the guide but could not confirm. (Lead-ins never written out have nothing on the page to doubt; an
    unchecked teaching addition is never published — the guide is rewritten from the source without it.)"""
    return [{"kind": "coverage", "source": f.source_excerpt[:80], "guide": f.draft_excerpt[:80]} for f in findings
            if f.code == "coverage_unconfirmed" and f.severity == "minor" and f.status == "unconfirmed" and f.draft_excerpt]


def gate2_program(source: str, guide: str, claims: list[SourceClaim], bridges: BridgePlan, bridges_used: list[str],
                  task, symbol_evidence: dict | None = None) -> list[ra.Finding]:
    """Program checks of the 主要正文 alone (formulas, KaTeX, independence from the 中文課文, process leaks), the
    formulas read from PDF regions (each must be in the guide), and the bridge rule: a 補充說明 paragraph needs an
    approved supplement bridge."""
    fs = ra.validate_draft(task, ra.GuideDraft(guide=guide or "（空）", chinese_text="（不檢查）"))
    fs = [f for f in fs if f.target == "guide"]
    ev_math = evidence_formulas(symbol_evidence)
    if ev_math:
        fs += [f.model_copy(update={"detail": f.detail.replace("主要正文沒有原樣保留", "主要正文沒有寫出依 PDF 版面取證的")})
               for f in ra._check_formulas(" ".join(f"$${x}$$" for x in ev_math), guide or "", "guide", "formula_not_verbatim")]
    # a 補充說明 label is not an approval: with no approved bridge at all it is a fact; with bridges, the audit must
    # tie its content to one (an addition that matches no bridge is reported there)
    if re.search(r"補充說明", guide or "") and not bridges.bridges:
        fs.append(ra.Finding(code="unapproved_supplement", severity="major", origin="program",
                             detail="正文有「補充說明」，但沒有任何核准的教學橋梁；原文之外的內容必須先經橋梁規劃核准",
                             suggested_fix="刪除「補充說明」段落"))
    elif bridges.bridges:
        # a 補充說明 paragraph must carry an approved bridge's content. Overlap is only used to *raise* this flag
        # (never to dismiss a reviewer's issue): a paragraph sharing little with every bridge is unapproved content.
        for para in re.split(r"\n\s*\n", guide or ""):
            body = re.sub(r"^[*_\s]*補充說明[*_\s]*[:：]?[*_\s]*", "", para.strip())
            if body != para.strip() and body and max(ra_overlap(body, b.content) for b in bridges.bridges) < SUPPLEMENT_MATCH:
                fs.append(ra.Finding(code="unapproved_supplement", severity="major", origin="program",
                                     draft_excerpt=body[:80],
                                     detail=f"「補充說明」的內容不屬於任何核准的教學橋梁：{body[:60]}",
                                     suggested_fix="刪除這段「補充說明」；只有核准橋梁的內容可以放進正文"))
    fs += copied_source(source, guide)
    return fs


COPIED_MIN_LETTERS = 30   # a source sentence with at least this many letters, found as is in the guide, was pasted


def copied_source(source: str, guide: str) -> list[ra.Finding]:
    """The 主要正文 of a non-Chinese source is written in Chinese. A small model often pastes the English source
    into it (a whole 區塊, or before its own explanation); the audit then finds every claim "covered" by the quoted
    English sentences. A source sentence found word for word in the guide is a program fact, whatever the audit
    says. Formulas are not counted (they are copied on purpose)."""
    if is_chinese(source):
        return []
    g = re.sub(r"\s+", " ", _strip_formulas(guide or "")).lower()
    pasted = []
    for _, s in source_segments(source):
        t = re.sub(r"\s+", " ", _strip_formulas(s)).strip().lower()
        if len(re.findall(r"[a-z]", t)) >= COPIED_MIN_LETTERS and t.rstrip(".;:!?") in g:
            pasted.append(s.strip())
    if not pasted:
        return []
    return [ra.Finding(code="source_copied", severity="major", origin="program", draft_excerpt=pasted[0][:80],
                       detail=f"主要正文照抄了 {len(pasted)} 句英文原文（例如「{pasted[0][:60]}」），必須用繁體中文寫",
                       suggested_fix="刪除照抄的英文原文，改用繁體中文完整寫出這些內容")]


def _strip_formulas(text: str) -> str:
    return re.sub(r"\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`", " ", text or "", flags=re.S)


def verbatim_covered(c: SourceClaim, guide: str, source: str) -> bool:
    """Coverage the program can prove without a model: the guide has a paragraph made only of whole source sentences
    (for a non-Chinese source: of this claim's audited translation), the claim's whole text is in that paragraph,
    and so are its formulas. Whether a statement is asserted, denied (「並非 X」), attributed (「有人誤以為 X」) or
    refuted afterwards (「X。這個說法不正確。」) is a matter of meaning, so the fast path only takes paragraphs with
    nothing but source text in them; anything else goes to the auditor. Content elsewhere in the guide (a rebuttal
    in another paragraph included) is still audited as an addition, and coherence is still audited."""
    text = c.source_text if is_chinese(source) else c.statement
    p = _plain(text)
    if len(p) < 8:
        return False
    allowed = {_plain(x) for _, x in source_segments(source if is_chinese(source) else c.statement)}
    for para in re.split(r"\n\s*\n", guide or ""):
        if p not in _plain(para):
            continue
        sents = [x for _, x in source_segments(para)]
        if sents and all(_plain(x) in allowed for x in sents):   # every sentence is a whole source sentence
            break
    else:
        return False
    gn = ra._norm_math(guide)
    need = [ra._norm_math(f) for f in c.formulas] + [f for f in sum(ra.source_formulas(c.source_text), []) if ra._is_real_math(f)]
    return all(f in gn for f in need if f)


# words that only restate a verdict; support made of these (and ids / punctuation) explains nothing
GENERIC_SUPPORT_RE = re.compile(r"這|是|個|的|很|正確|無誤|沒錯|對的|成立|先備|知識|常識|基本|基礎|標準|定義|事實|"
                                r"眾所周知|顯然|明顯|一般|通常|本來|就|如此|因為|所以|"
                                r"\b(?:this|it|is|correct|true|right|standard|basic|common|knowledge|prerequisite|"
                                r"obvious(?:ly)?|fact|well|known|definition)\b", re.I)


def _substance(text: str) -> str:
    t = re.sub(r"\bS\d+\b|\[[^\]]*\]|[A-Za-z0-9_-]+:C\d+", " ", text or "")
    t = GENERIC_SUPPORT_RE.sub("", t)
    return re.sub(r"[\s，。、；：！？,.;:!?「」『』（）()\"'*`-]+", "", t)


def bridge_support_problem(basis: str, support: str, source: str, refs: dict | None = None) -> str:
    """Why the stated support of a bridge's correctness cannot be checked ("" = it has the required, checkable
    parts). Used by the body audit and by verify alike, so a re-check cannot replace a missing citation or
    derivation with a bare "it is correct". `refs`: {"read": {"unit:claim", …} the reader has read,
    "reader": the reader's own words (goal and background)}.

    - source: source sentence ids, every one of which exists;
    - derivation: the premises as existing source sentence ids, plus the step written out (not a verdict);
    - prior_reading: read claims cited as unit:claim, every one of which the reader has really read;
    - user_authorized: passages quoted in 「」 from what the reader asked for (goal / background), every one of them;
    - prerequisite: what the knowledge is and why it is standard, in words beyond a verdict. Nothing outside the
      source can be checked by the program here, so passing this only makes it a recorded model judgement."""
    refs = refs or {}
    support = support or ""
    segs = {sid for sid, _ in source_segments(source)}
    cited = set(re.findall(r"\bS\d+\b", support))
    bad = sorted(cited - segs)
    if bad:
        return f"引用的原文句子不存在：{'、'.join(bad)}"
    if basis == "source":
        return "" if cited else "依據原文的橋梁沒有引用原文句子編號"
    if basis == "derivation":
        if not cited:
            return "推導沒有引用作為前提的原文句子編號"
        return "" if len(_substance(support)) >= 6 else "推導沒有寫出推導步驟"
    if basis == "prior_reading":
        read = set(refs.get("read") or ())
        named = set(re.findall(r"[A-Za-z0-9_-]+:C\d+", support))
        if not named:
            return "依據已讀段落的橋梁沒有引用已讀命題（單元:命題編號）"
        unread = sorted(named - read)
        return f"引用的已讀命題不在讀者讀過的內容中：{'、'.join(unread)}" if unread else ""
    if basis == "user_authorized":
        reader = _plain(refs.get("reader") or "")
        quotes = re.findall(r"「([^」]*)」", support)
        if not reader or not quotes:
            return "讀者授權的橋梁沒有用「」引用讀者提出的要求"
        bad = [q for q in quotes if len(_plain(q)) < 4 or _plain(q) not in reader]
        return f"引用的內容不是讀者的原話：{'、'.join(bad)}" if bad else ""
    return "" if len(_substance(support)) >= 6 else "沒有說明這是什麼先備知識、為何是標準內容"


def coverage_findings(source: str, guide: str, claims: list[SourceClaim], bridges: BridgePlan,
                      audit: BodyAudit, refs: dict | None = None) -> list[ra.Finding]:
    """Body audit → findings with checked evidence (see module doc)."""
    out: list[ra.Finding] = []
    by_id = {c.id: c for c in claims}
    basis = restated_source(claims, source)
    for v in audit.claims:
        c = by_id.get(v.id)
        if c is None:
            continue
        cond = c.function in ("condition", "exception") or c.conditions or c.exceptions
        if v.verdict == "covered":
            full = ra.expand_quote(guide, v.guide_quote, 6)
            quoted = bool(full)
            lost = coverage_parity(restated(c, source), full, basis) if quoted else []
            needed = [ra._norm_math(f) for f in c.formulas] + [f for f in sum(ra.source_formulas(c.source_text), [])
                                                                 if ra._is_real_math(f)]
            gn = ra._norm_math(guide)
            miss = [f for f in needed if f and f not in gn]   # formulas anywhere in the guide (a claim may span sentences)
            if quoted and not lost and not miss:
                continue
            why = ("引用的句子不在主要正文" if not quoted else "；".join(lost) if lost else f"正文沒有公式 {miss}")
            out.append(unconfirmed_coverage(c, guide, source, basis, f"的涵蓋無法確認：{why}", v.guide_quote,
                                            formulas_missing=bool(miss)))
        elif v.verdict == "unsure":
            out.append(unconfirmed_coverage(c, guide, source, basis, f"是否涵蓋無法判斷：{v.note[:60]}"))
        else:
            code = "meaning_changed" if v.verdict == "distorted" else ("missing_condition" if cond else "missing_key_claim")
            out.append(ra.Finding(code=code, severity="major", origin="review", item=c.id, source_excerpt=c.source_text,
                                  draft_excerpt=v.guide_quote if ra.contains_quote(guide, v.guide_quote, 2) else "",
                                  detail=f"{c.id}「{c.statement[:60]}」{'在正文中被改變' if v.verdict == 'distorted' else '沒有寫進正文'}"
                                         + (f"：{v.note[:80]}" if v.note else ""), suggested_fix=c.statement))
    bmap = {b.id: b for b in bridges.bridges}
    for v in audit.bridges:
        b = bmap.get(v.id)
        if b is None:
            continue
        if v.verdict == "absent":
            out.append(ra.Finding(code="bridge_missing", severity="major", origin="review", item=b.id,
                                  detail=f"核准的教學橋梁 {b.id} 沒有寫進正文：{b.content[:60]}", suggested_fix=b.content))
        elif v.verdict == "incorrect":
            out.append(ra.Finding(code="bridge_incorrect", severity="major", origin="review", item=b.id,
                                  draft_excerpt=v.guide_quote, detail=f"教學橋梁 {b.id} 的寫法不正確：{v.note[:80]}"))
        elif v.verdict == "unnecessary":
            out.append(ra.Finding(code="bridge_questioned", severity="minor", origin="review", item=b.id,
                                  detail=f"檢查認為 {b.id} 可能不必要：{v.note[:80]}"))
        # where the guide really carries the bridge, found by the program (the auditor's quote is only a hint)
        _, located = best_match(b.content, guide)
        written = ra_overlap(b.content, located) >= SUPPLEMENT_MATCH
        if v.verdict in ("ok", "unnecessary"):
            # present: the quoted guide passage must exist and carry the bridge's content. An approved bridge that is
            # not written in leaves a faithful guide without one teaching aid: recorded, never a reason to rewrite
            full = ra.expand_quote(guide, v.guide_quote, 6)
            if not full or ra_overlap(b.content, full) < SUPPLEMENT_MATCH:
                out.append(ra.Finding(code="bridge_unconfirmed", severity="minor", status="unconfirmed", origin="review",
                                      item=b.id, source_excerpt=b.content, draft_excerpt=located if written else "",
                                      detail=f"教學橋梁 {b.id} 是否寫進正文無法確認："
                                             + ("引用的句子不在主要正文" if not full else "引用的句子與橋梁內容不符")))
        if v.verdict != "absent":
            # correct: judged on its own, with support the program can at least check the shape of
            support_ok = not bridge_support_problem(b.basis, v.support, source, refs)
            if v.correct == "no":
                out.append(ra.Finding(code="bridge_incorrect", severity="major", origin="review", item=b.id,
                                      detail=f"教學橋梁 {b.id} 的內容不正確：{v.note[:80] or b.content[:60]}",
                                      suggested_fix="改正或刪除這項補充內容"))
            elif v.correct == "unsure" or not support_ok:
                # only what is on the page can mislead: a bridge written in stays blocking (re-check, then the guide is
                # written again from the source alone); one that never reached the guide is only recorded
                out.append(ra.Finding(code="bridge_correctness_unconfirmed", severity="major" if written else "minor",
                                      status="unconfirmed", origin="review", item=b.id, source_excerpt=b.content,
                                      basis=b.basis,
                                      detail=f"教學橋梁 {b.id} 的正確性沒有可核對的依據"
                                             + ("（依據原文的橋梁必須引用存在的原文句子編號）" if b.basis == "source" else "")
                                             + ("" if written else "（這項橋梁沒有寫進正文）")))
    kinds = {"unsupported": "unsupported_claim", "incorrect": "unsupported_claim", "process": "process_leak",
             "narration": "narration", "redirect": "redirect_to_source"}
    for a in audit.additions:
        code = kinds[a.kind]
        real = ra.contains_quote(guide, a.guide_quote, 4)
        out.append(ra.Finding(code=code, severity=ra.severity_of(code), origin="review",
                              status="confirmed" if real else "unconfirmed", draft_excerpt=a.guide_quote,
                              detail=f"正文的新增內容有問題：{a.note[:80] or a.kind}" + ("" if real else "（引用的片段不在正文中）")))
    for passage in audit.uncovered_source:
        if ra.contains_quote(source, passage, 4):
            out.append(ra.Finding(code="missing_key_claim", severity="major", origin="review", source_excerpt=passage,
                                  detail=f"正文沒有涵蓋原文：「{passage[:60]}」"))
        else:   # a "missing source passage" that is not in the source refutes itself; recorded, not blocking
            out.append(ra.Finding(code="review_citation_invalid", severity="minor", origin="review", source_excerpt=passage,
                                  detail=f"檢查指出的原文不在原文中，已忽略：「{passage[:40]}」"))
    if audit.coherence == "fail":
        out.append(ra.Finding(code="incoherent", severity="major", origin="review",
                              detail=f"只讀主要正文時不連貫或讀不懂：{audit.coherence_note[:100]}", suggested_fix=audit.coherence_note))
    return out


def gate2_result(claims: list[SourceClaim], bridges: BridgePlan, findings: list[ra.Finding]) -> GateResult:
    """The persisted record: one check per claim and bridge, plus the program / addition / coherence items."""
    blocking = [f for f in findings if f.severity == "major"]
    checks = []
    for c in claims:
        bad = [f for f in blocking if f.item == c.id]
        checks.append(GateCheck(id=f"coverage:{c.id}", kind="coverage", status="fail" if bad else "pass",
                                detail="；".join(f.detail for f in bad)[:200]))
    for b in bridges.bridges:
        bad = [f for f in blocking if f.item == b.id]
        checks.append(GateCheck(id=f"bridge:{b.id}", kind="bridge", status="fail" if bad else "pass",
                                detail="；".join(f.detail for f in bad)[:200]))
    other = [f for f in blocking if not f.item]
    checks.append(GateCheck(id="body:other", kind="body", status="fail" if other else "pass",
                            detail="；".join(f"[{f.code}] {f.detail}" for f in other)[:300]))
    return GateResult.of("gate2", checks)


def symbol_like(text: str) -> bool:
    return bool(MATH_HINT_RE.search(text or "")) or "$" in (text or "")


__all__ = ["source_segments", "gate1_program", "gate1_apply_audit", "gate1_decide", "needs_source_audit",
           "audit_items", "audit_check", "bridge_check", "body_audit_check", "gate2_program", "coverage_findings",
           "gate2_result", "notation_check", "CATEGORIES", "profile"]
