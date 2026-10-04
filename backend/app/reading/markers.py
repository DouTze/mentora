"""Strength markers of a proposition (negation, condition, exception, quantifier, modality) in Chinese and English,
and a parity check between a source passage and the passage that claims to restate it.

This is a program aid, not a proof of meaning. It finds the cheap, frequent failures — a dropped "only if", an
added "並不", "all" restated without any quantifier — and turns them into *unconfirmed* items that a focused model
check or a revision must settle. It never confirms that two passages mean the same: passing parity only says no
marker category was visibly lost or added. Text overlap (bigrams, substrings) is deliberately not used here; a
reversed sentence overlaps its source almost completely."""
from __future__ import annotations

import re

MARKERS_VERSION = "markers-v1"

# words that contain a negation character but do not negate the predicate
ZH_NEG_EXEMPT = ("不同", "不僅", "不斷", "不過", "不管", "不論", "不只", "無論", "不少", "非常", "無數", "別人", "別的",
                 "否則", "未來", "不久", "不但", "無關緊要", "並非只", "不外乎", "無疑", "毫無疑問", "非但")
ZH_NEG_RE = re.compile(r"不|沒|無|非|未|否|勿|別|毋")
ZH = {
    "condition": re.compile(r"若|如果|假如|假設|只有|只要|才|當|除非|前提|條件|一旦|否則|倘若|給定|在[^。，；]{0,12}(?:下|時)"),
    "exception": re.compile(r"除了|除非|例外|除外|但是|但|然而|不過|卻"),
    "quantifier": re.compile(r"所有|全部|每|任何|任一|一切|皆|都|一些|某些|部分|有些|至少|至多|最多|最少|大多|多數|少數|唯一|僅|只"),
    "modality": re.compile(r"必須|一定|必然|必定|可能|也許|或許|可以|能夠|能|應該|應|總是|永遠|絕不|從不|往往|通常|未必|保證|需要|須"),
}
EN = {
    "negation": re.compile(r"\b(?:not|no|never|none|neither|nor|without|cannot|nothing|nobody)\b|n't\b", re.I),
    "condition": re.compile(r"\b(?:if|unless|when|whenever|provided|assuming|as long as|given that|in case|otherwise|only if|only when)\b", re.I),
    "exception": re.compile(r"\b(?:except|unless|but|however|although|though|apart from|other than)\b", re.I),
    "quantifier": re.compile(r"\b(?:all|every|each|any|some|none|most|few|many|at least|at most|only|both|either)\b", re.I),
    "modality": re.compile(r"\b(?:must|may|might|can|could|should|necessarily|always|never|guarantees?|guaranteed|likely|possibly|certainly|need|needs|required?)\b", re.I),
}
CATEGORIES = ("negation", "condition", "exception", "quantifier", "modality")
NAMES = {"negation": "否定", "condition": "條件／前提", "exception": "例外／轉折", "quantifier": "量詞／範圍",
         "modality": "必要性／可能性"}


def _strip_math(text: str) -> str:
    """Formulas and code are not prose: `if` in a list comprehension is not a condition of the proposition."""
    return re.sub(r"```.*?(?:```|$)|\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`", " ", text or "", flags=re.S)


def zh_negations(text: str) -> list[str]:
    t = _strip_math(text)
    exempt = {i for w in ZH_NEG_EXEMPT for m in re.finditer(re.escape(w), t) for i in range(m.start(), m.end())}
    return [m.group(0) for m in ZH_NEG_RE.finditer(t) if m.start() not in exempt]


def profile(text: str) -> dict[str, list[str]]:
    """Category → the marker words found (Chinese and English rules both apply; mixed text is common)."""
    t = _strip_math(text)
    found: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    found["negation"] += zh_negations(t)
    for cat, rx in ZH.items():
        found[cat] += rx.findall(t)
    for cat, rx in EN.items():
        found[cat] += [m.group(0).lower() for m in rx.finditer(t)]
    return {c: v for c, v in found.items() if v}


def parity(source: str, target: str, *, categories: tuple[str, ...] = CATEGORIES) -> list[str]:
    """Visible marker differences from `source` to `target` (its restatement / translation), as short Chinese notes.
    Empty = no category visibly dropped or (for negation) added. Not a semantic equivalence check."""
    s, t = profile(source), profile(target)
    out = []
    for cat in categories:
        if s.get(cat) and not t.get(cat):
            out.append(f"來源有{NAMES[cat]}（{'、'.join(dict.fromkeys(s[cat]))}），對應文字沒有")
    if "negation" in categories and t.get("negation") and not s.get("negation"):
        out.append(f"對應文字多了否定（{'、'.join(dict.fromkeys(t['negation']))}），來源沒有")
    return out
