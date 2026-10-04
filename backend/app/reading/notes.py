"""Notes and questions from verified content (R6).

Notes are compressed from the *verified claims* of a part (Gate 1 passed), never re-summarised from raw text. Each
note cites the claims it compresses; the program checks that the cited claims exist, that every condition /
negation / exception / quantifier of what it cites survives (markers.parity) and that their formulas are kept; a
model audit then judges each note against its claims. Only notes that pass both are `verified`. Verified claims no
verified note covers stay available as they are, so compression never drops part of what was read.

Questions start from a program coverage plan (topics = parts / sections of the read range, the ones with open
questions first), cite basis ids (verified notes or verified claims of read units only), and are audited for being
answerable from their basis with the right conditions. A question without such a basis is not asked."""
from __future__ import annotations

import re

from . import agent as ra
from .contracts import NoteItem, NotesAudit, NotesOutput, QuizAudit, QuizDraft, VerifiedNote
from .markers import parity

NOTES_VERSION = "notes-v1"
# claim functions a part's notes must keep (examples, transitions and questions may be left to the guide)
KEEP = {"definition", "claim", "condition", "exception", "formula", "reasoning", "procedure", "warning", "comparison",
        "counterexample"}


def part_claims(quotes: list[dict], guides: dict, lo: int, hi: int) -> dict[str, dict]:
    """"U{k}.C{n}" → {unit_id, index, claim} for units lo..hi (inclusive) whose guide passed Gate 1."""
    out = {}
    for k, i in enumerate(range(lo, hi + 1), 1):
        g = guides.get(str(i)) or {}
        for c in g.get("source_claims") or []:
            out[f"U{k}.{c['id']}"] = {"unit_id": quotes[i]["unit_id"], "index": i, "claim": c}
    return out


def claims_listing(cmap: dict[str, dict]) -> str:
    lines = []
    for key, x in cmap.items():
        c = x["claim"]
        extra = "；".join(filter(None, ["條件：" + "；".join(c.get("conditions") or []) if c.get("conditions") else "",
                                         "例外：" + "；".join(c.get("exceptions") or []) if c.get("exceptions") else "",
                                         "公式：" + "；".join(c.get("formulas") or []) if c.get("formulas") else ""]))
        lines.append(f"- {key}［{c.get('function', 'claim')}］{c['statement']}" + (f"（{extra}）" if extra else ""))
    return "\n".join(lines)


def notes_schema(cmap: dict[str, dict]) -> dict:
    """claim_refs restricted to the part's claim keys; at most a little more than one note per claim."""
    sch = NotesOutput.model_json_schema()
    item = dict(sch["$defs"]["NoteItem"])
    item["properties"] = {**item["properties"], "claim_refs": {"type": "array", "minItems": 1, "maxItems": 6,
                                                               "items": {"type": "string", "enum": list(cmap) or ["-"]}}}
    sch["properties"]["notes"] = {"type": "array", "items": item, "minItems": 1, "maxItems": len(cmap) + 2}
    return sch


def quiz_schema(plan: list[dict], basis_ids: list[str]) -> dict:
    """Exactly one concept per planned topic; basis ids only from the read range's verified basis."""
    sch = QuizDraft.model_json_schema()
    defs = sch["$defs"]
    ids = {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string", "enum": basis_ids or ["-"]}}
    q = dict(defs["QuizQuestion"])
    q["properties"] = {**q["properties"], "basis": ids}
    c = dict(defs["QuizConcept"])
    c["properties"] = {**c["properties"], "topic": {"type": "string", "enum": [t["id"] for t in plan] or ["-"]},
                       "basis": ids, "diagnostic": q, "transfer": q}
    sch["properties"]["concepts"] = {"type": "array", "items": c, "minItems": len(plan), "maxItems": len(plan)}
    return sch


def notes_check(cmap: dict[str, dict]):
    from ..llm.tasks import OutputInvalid

    def check(out: NotesOutput) -> None:
        bad = sorted({r for n in out.notes for r in n.claim_refs if r not in cmap})
        ids = [n.id for n in out.notes]
        if bad or len(ids) != len(set(ids)):
            raise OutputInvalid((f"claim_refs 只能用這些編號：{'、'.join(cmap)}（不存在 {bad}）" if bad else "")
                                + ("；筆記編號重複" if len(ids) != len(set(ids)) else ""))
    return check


def _cited_text(note: NoteItem, cmap: dict) -> tuple[str, list[str]]:
    cs = [cmap[r]["claim"] for r in note.claim_refs if r in cmap]
    text = " ".join(" ".join([c["statement"], *(c.get("conditions") or []), *(c.get("exceptions") or []), c.get("strength", "")])
                    for c in cs)
    formulas = [ra._norm_math(f) for c in cs for f in c.get("formulas") or []]
    formulas += [f for c in cs for f in sum(ra.source_formulas(c.get("source_text") or ""), []) if ra._is_real_math(f)]
    return text, sorted(set(f for f in formulas if f))


def verify_notes(out: NotesOutput, cmap: dict[str, dict], audit: NotesAudit | None) -> list[VerifiedNote]:
    verdict = {a.id: a for a in (audit.notes if audit else [])}
    res = []
    for n in out.notes:
        cited, formulas = _cited_text(n, cmap)
        mine = " ".join([n.text, *n.conditions])
        reasons = [f"壓縮後{x}" for x in parity(cited, mine)]
        have = ra._norm_math(mine + " " + " ".join(n.formulas))
        miss = [f for f in formulas if f not in have]
        if miss:
            reasons.append(f"缺少公式 {miss}")
        a = verdict.get(n.id)
        if a is None:
            reasons.append("沒有核對結果")
        elif a.verdict != "faithful":
            reasons.append(f"核對：{a.verdict}" + (f"（{a.note[:60]}）" if a.note else ""))
        res.append(VerifiedNote(id=n.id, kind=n.kind, term=n.term, text=n.text, claim_refs=n.claim_refs,
                                unit_ids=sorted({cmap[r]["unit_id"] for r in n.claim_refs if r in cmap}),
                                conditions=n.conditions, formulas=n.formulas,
                                status="unverified" if reasons else "verified", reasons=reasons))
    return res


def coverage_gaps(notes: list[VerifiedNote], cmap: dict[str, dict]) -> list[str]:
    cited = {r for n in notes if n.status == "verified" for r in n.claim_refs}
    return [k for k, x in cmap.items() if x["claim"].get("function", "claim") in KEEP and k not in cited]


def notes_issues(notes: list[VerifiedNote], gaps: list[str], cmap: dict[str, dict]) -> str:
    lines = [f"- {n.id}：{'；'.join(n.reasons)}" for n in notes if n.status != "verified"]
    lines += [f"- 沒有筆記涵蓋 {k}：{cmap[k]['claim']['statement'][:60]}" for k in gaps]
    return "\n".join(lines)


def notes_markdown(part: dict, notes: list[VerifiedNote], gaps: list[str], cmap: dict[str, dict]) -> str:
    """Reader notes, built by the program from verified notes only (plus verified claims no note covers)."""
    ok = [n for n in notes if n.status == "verified"]
    lines = [f"### {part['label']}", ""]
    points = [n for n in ok if n.kind == "point"]
    if points or gaps:
        lines += ["**本節重點整理**", ""] + [f"- {n.text}" for n in points] + [f"- {cmap[k]['claim']['statement']}" for k in gaps]
        lines.append("")
    conf = [n for n in ok if n.kind == "confusion"]
    if conf:
        lines += ["**易混淆觀念與提醒**", ""] + [f"- {n.text}" for n in conf] + [""]
    terms = [n for n in ok if n.kind == "term"]
    if terms:
        lines += ["**本節核心術語**", ""] + [f"- **{n.term or n.text[:12]}**：{n.text}" for n in terms] + [""]
    return "\n".join(lines).strip() + "\n"


# -- questions -----------------------------------------------------------------------------------------

def quiz_basis(content: dict, rd: dict, upto: int) -> list[dict]:
    """Basis of a quiz over quotes 0..upto: verified notes of compacted read parts, and the verified claims of read
    units that no verified note covers. Units whose guide did not pass contribute nothing (no unverified basis)."""
    quotes, guides, compacts = content["quotes"], rd["guides"], rd.get("compacts") or {}
    out, covered = [], set()
    for p in content["parts"]:
        if p["quote_from"] > upto:
            break
        c = compacts.get(str(p["index"])) or {}
        for n in c.get("notes") or []:
            if n.get("status") == "verified":
                out.append({"id": f"P{p['index'] + 1}.{n['id']}", "kind": "note", "text": n["text"],
                            "part": p["index"], "unit_ids": n["unit_ids"]})
                covered |= {(p["index"], r) for r in n["claim_refs"]}
        for k, i in enumerate(range(p["quote_from"], min(p["quote_to"], upto) + 1), 1):
            g = guides.get(str(i)) or {}
            if g and "source_claims" not in g:   # read under reading-graph-v1: no verified claims, the source itself
                out.append({"id": f"Q{i + 1}.SRC", "kind": "source", "text": quotes[i]["text"], "part": p["index"],
                            "unit_ids": [quotes[i]["unit_id"]]})
                continue
            for cl in g.get("source_claims") or []:
                if (p["index"], f"U{k}.{cl['id']}") in covered:
                    continue
                out.append({"id": f"Q{i + 1}.{cl['id']}", "kind": "claim", "text": cl["statement"],
                            "part": p["index"], "unit_ids": [quotes[i]["unit_id"]], "function": cl.get("function")})
    return out


def coverage_plan(content: dict, rd: dict, basis: list[dict], upto: int, max_topics: int) -> list[dict]:
    """Topics = read parts that have basis; parts the reader asked about come first, then the larger ones; at most
    `max_topics` (the rest are reported as not covered by this quiz)."""
    asked = {int(k) for k, v in (rd.get("qa") or {}).items() if v and int(k) <= upto}
    topics = []
    for p in content["parts"]:
        ids = [b["id"] for b in basis if b["part"] == p["index"]]
        if not ids:
            continue
        qs = [x["question"] for i in range(p["quote_from"], min(p["quote_to"], upto) + 1) for x in (rd.get("qa") or {}).get(str(i), [])]
        topics.append({"label": p["label"] + (f"（{' › '.join(p.get('section') or [])}）" if p.get("section") else ""),
                       "basis": ids, "questions": qs[-3:], "asked": bool(asked & set(range(p["quote_from"], p["quote_to"] + 1)))})
    topics.sort(key=lambda t: (not t["asked"], -len(t["basis"])))
    chosen = topics[:max_topics]
    for k, t in enumerate(chosen, 1):
        t["id"] = f"T{k}"
    return chosen


def quiz_check(plan: list[dict], basis_ids: set[str]):
    from ..llm.tasks import OutputInvalid

    def check(d: QuizDraft) -> None:
        errs = []
        want = [t["id"] for t in plan]
        got = [c.topic for c in d.concepts]
        missing = [t for t in want if t not in got]
        if missing or len(got) != len(set(got)) or set(got) - set(want):
            errs.append(f"每個主題剛好一個概念：{'、'.join(want)}" + (f"（缺少 {missing}）" if missing else ""))
        allowed = {t["id"]: set(t["basis"]) for t in plan}
        for c in d.concepts:
            for where, ids in (("概念", c.basis), ("診斷題", c.diagnostic.basis), ("練習題", c.transfer.basis)):
                bad = [x for x in ids if x not in basis_ids]
                if bad:
                    errs.append(f"{c.topic} 的{where} basis 只能用依據清單中的編號（不存在 {bad}）")
                elif c.topic in allowed and not set(ids) & allowed[c.topic]:
                    errs.append(f"{c.topic} 的{where}必須引用這個主題自己的依據")
        if errs:
            raise OutputInvalid("；".join(errs))
    return check


def quiz_audit_ids(d: QuizDraft) -> list[str]:
    return [f"{c.topic}.{k}" for c in d.concepts for k in ("diagnostic", "transfer")]


def quiz_keep(d: QuizDraft, audit: QuizAudit) -> tuple[QuizDraft, list[str]]:
    """Concepts whose two questions both pass the audit; the reasons for the ones dropped."""
    verdict = {a.id: a for a in audit.questions}
    keep, dropped = [], []
    for c in d.concepts:
        bad = [f"{c.topic}.{k}：{verdict[f'{c.topic}.{k}'].verdict}" for k in ("diagnostic", "transfer")
               if verdict.get(f"{c.topic}.{k}") is None or verdict[f"{c.topic}.{k}"].verdict != "ok"]
        if bad:
            dropped.append(f"{c.title}（{'；'.join(bad)}）")
        else:
            keep.append(c)
    return d.model_copy(update={"concepts": keep}), dropped


def basis_listing(basis: list[dict]) -> str:
    kinds = {"note": "筆記", "claim": "命題", "source": "原文"}
    return "\n".join(f"- {b['id']}［{kinds[b['kind']]}］{b['text']}" for b in basis)


def plan_listing(plan: list[dict]) -> str:
    out = []
    for t in plan:
        out.append(f"- {t['id']}：{t['label']}；依據 {'、'.join(t['basis'])}"
                   + (f"；學生問過：{'／'.join(t['questions'])}" if t["questions"] else ""))
    return "\n".join(out)


def symbol_consistency(notes_by_part: list[list[dict]]) -> list[str]:
    """Across completed parts: a letter written bold in one note and plain in another (a likely notation slip)."""
    bold, plain = set(), set()
    for notes in notes_by_part:
        for n in notes:
            math = " ".join(re.findall(r"\$[^$]+\$", n.get("text", "")))
            bold |= set(re.findall(r"\\mathbf\{([A-Za-z])\}", math))
            plain |= set(re.findall(r"(?<![\\A-Za-z{])([A-Za-z])(?![A-Za-z}])", re.sub(r"\\mathbf\{[A-Za-z]\}", " ", math)))
    return [f"符號 {x} 在不同部分有粗體與一般寫法，請確認是否為同一個數學物件" for x in sorted(bold & plain)]
