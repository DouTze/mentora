"""R6: notes compressed from verified claims and checked item by item; questions only from verified basis of the
read range, on a program coverage plan, each bound to its basis and audited."""
import pytest

from app.llm.tasks import OutputInvalid
from app.reading import notes
from app.reading.contracts import NoteAuditItem, NotesAudit, NotesOutput, QuizAudit, QuizDraft
from test_workflow import graph_app, idle, run, start  # noqa: F401  (fixture re-export)

QUOTES = [{"index": 0, "unit_id": "u0", "label": "第 1 行"}, {"index": 1, "unit_id": "u1", "label": "第 3 行"},
          {"index": 2, "unit_id": "u2", "label": "第 5 行"}]
GUIDES = {
    "0": {"source_claims": [{"id": "C1", "statement": "只有在樣本彼此獨立時，這個區間才有所述的涵蓋率。", "function": "condition",
                             "conditions": ["樣本彼此獨立"]},
                            {"id": "C2", "statement": "樣本平均數近似 $\\bar{x} \\sim N(\\mu, \\sigma^2/n)$。", "function": "formula",
                             "formulas": ["\\bar{x} \\sim N(\\mu, \\sigma^2/n)"]}]},
    "1": {"source_claims": [{"id": "C1", "statement": "例如抽 30 個樣本。", "function": "example"}]},
    # unit 2's guide did not pass: no verified claims
}


def audit(*verdicts):
    return NotesAudit(notes=[NoteAuditItem(id=f"N{i}", verdict=v) for i, v in enumerate(verdicts, 1)])


def test_a_note_that_drops_a_condition_or_a_formula_is_not_verified():
    cmap = notes.part_claims(QUOTES, GUIDES, 0, 1)
    assert set(cmap) == {"U1.C1", "U1.C2", "U2.C1"}
    out = NotesOutput(notes=[
        {"id": "N1", "text": "這個區間有所述的涵蓋率。", "claim_refs": ["U1.C1"]},                     # condition lost
        {"id": "N2", "text": "樣本平均數近似常態分布。", "claim_refs": ["U1.C2"]},                     # formula lost
        {"id": "N3", "text": "只有樣本彼此獨立時，區間才有涵蓋率。", "claim_refs": ["U1.C1"]}])
    vn = notes.verify_notes(out, cmap, audit("faithful", "faithful", "faithful"))
    assert [n.status for n in vn] == ["unverified", "unverified", "verified"]
    assert "條件" in vn[0].reasons[0] and "公式" in vn[1].reasons[0]
    assert notes.coverage_gaps(vn, cmap) == ["U1.C2"]      # the example may be left out, the formula may not


def test_a_fabricated_note_fails_the_audit_and_unknown_refs_are_refused():
    cmap = notes.part_claims(QUOTES, GUIDES, 0, 1)
    out = NotesOutput(notes=[{"id": "N1", "text": "樣本數越大，涵蓋率一定越高。", "claim_refs": ["U1.C1"]}])
    vn = notes.verify_notes(out, cmap, audit("unsupported"))
    assert vn[0].status == "unverified" and "unsupported" in vn[0].reasons[-1]
    vn = notes.verify_notes(out, cmap, None)
    assert vn[0].status == "unverified" and "沒有核對結果" in vn[0].reasons   # no audit is never a pass
    with pytest.raises(OutputInvalid):
        notes.notes_check(cmap)(NotesOutput(notes=[{"id": "N1", "text": "某條筆記", "claim_refs": ["U9.C1"]}]))


def content_and_rd(compact_notes):
    content = {"quotes": QUOTES, "parts": [{"index": 0, "quote_from": 0, "quote_to": 1, "label": "第 1–3 行", "section": ["抽樣"]},
                                           {"index": 1, "quote_from": 2, "quote_to": 2, "label": "第 5 行", "section": ["區間"]}]}
    rd = {"guides": GUIDES, "compacts": {"0": {"notes": compact_notes}}, "qa": {"2": [{"question": "為什麼？"}]}}
    return content, rd


def test_quiz_basis_is_verified_and_inside_the_read_range():
    verified = {"id": "N1", "status": "verified", "text": "只有樣本彼此獨立時，區間才有涵蓋率。", "claim_refs": ["U1.C1"], "unit_ids": ["u0"]}
    rejected = {"id": "N2", "status": "unverified", "text": "捏造", "claim_refs": ["U1.C2"], "unit_ids": ["u0"]}
    content, rd = content_and_rd([verified, rejected])
    basis = notes.quiz_basis(content, rd, upto=2)
    ids = [b["id"] for b in basis]
    assert "P1.N1" in ids and "P1.N2" not in ids              # an unverified note is never a basis
    assert "Q1.C2" in ids and "Q1.C1" not in ids              # the claim the rejected note failed to cover stays, as is
    assert not [b for b in basis if b["part"] == 1]           # unit 2's guide failed: nothing from it
    early = notes.quiz_basis(content, rd, upto=0)             # early quiz: only what was read
    assert {b["id"] for b in early} <= {"P1.N1", "Q1.C2"} and "Q2.C1" not in {b["id"] for b in early}


def test_coverage_plan_and_question_checks():
    content, rd = content_and_rd([])
    rd["guides"] = {**GUIDES, "2": {"source_claims": [{"id": "C1", "statement": "區間是隨機的。", "function": "claim"}]}}
    basis = notes.quiz_basis(content, rd, upto=2)
    plan = notes.coverage_plan(content, rd, basis, 2, max_topics=3)
    assert [t["label"].split("（")[0] for t in plan] == ["第 5 行", "第 1–3 行"]   # the part the reader asked about first
    ids = {b["id"] for b in basis}
    q = lambda b: {"prompt": "請說明理由", "rubric": "需提到條件", "hints": [], "basis": b}
    good = QuizDraft(title="t", concepts=[
        {"topic": "T1", "title": "a", "explanation": "解釋解釋", "basis": ["Q3.C1"], "diagnostic": q(["Q3.C1"]), "transfer": q(["Q3.C1"])},
        {"topic": "T2", "title": "b", "explanation": "解釋解釋", "basis": ["Q1.C1"], "diagnostic": q(["Q1.C1"]), "transfer": q(["Q1.C2"])}])
    notes.quiz_check(plan, ids)(good)
    wrong_topic = good.model_copy(deep=True)
    wrong_topic.concepts[0].diagnostic.basis = ["Q1.C1"]       # T1's question resting only on T2's basis
    with pytest.raises(OutputInvalid):
        notes.quiz_check(plan, ids)(wrong_topic)
    kept, dropped = notes.quiz_keep(good, QuizAudit(questions=[
        {"id": "T1.diagnostic", "verdict": "ok"}, {"id": "T1.transfer", "verdict": "wrong_condition"},
        {"id": "T2.diagnostic", "verdict": "ok"}, {"id": "T2.transfer", "verdict": "ok"}]))
    assert [c.topic for c in kept.concepts] == ["T2"] and "wrong_condition" in dropped[0]


def test_session_notes_and_questions_carry_their_basis(graph_app):
    c = graph_app
    s = start(c)
    sid = s["id"]
    for k in range(len(s["reading"]["quotes"])):
        s, cmd = run(c, sid, "next", f"n{k}")
    assert cmd["status"] == "completed" and s["status"] == "active"
    row = c.app_ref.state.db.get_session(sid)
    rd = row["state"]["reading"]
    compacts = list(rd["compacts"].values())
    assert compacts and all(n["status"] == "verified" and n["claim_refs"] for cp in compacts for n in cp["notes"])
    content = row["content_snapshot"]
    assert content["concepts"] and all(cc["basis"] and cc["source_refs"] for cc in content["concepts"])
    assert all(q["basis"] for q in content["questions"])
    assert rd["quiz_source"] == "verified" and rd["quiz_plan"]["topics"]
