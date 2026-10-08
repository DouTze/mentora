"""Program rules of the handout review (reading-graph-v3), each from a case seen with qwen3-vl:4b: what the program
treats as a fact (a reprint of the textbook, a missing equation, a reviewer's quotation that is not there) and what
it leaves to the reviewers."""
from app.reading import agent as ra
from app.reading import gates
from app.reading.contracts import Conflict, ConflictsReview, Edit, PointsReview, PointVerdict

ZH = ("自然語言處理是AI 領域中最具挑戰性之一，旨在使機器能夠理解、解釋並生成人類語言。隨著語音助理、聊天機器人及智慧客服等應用的"
      "普及，NLP 技術的發展已經從簡單的語言分析進階到複雜的語意理解。")
EN = ("We introduce key elements of the problem's mathematical structure, such as returns, value functions, and Bellman "
      "equations. Whereas in bandit problems we estimated the value $q_*(a)$ of each action $a$, in MDPs we estimate the "
      "value of each action in each state.")


def task(text: str, **kw) -> ra.ReadingTaskInput:
    return ra.ReadingTaskInput(session_id="s", unit_id="u", index=0, total=1, label="l", text=text, material_title="m",
                               range_label="r", model_config_hash="h", prompt_bundle_hash="p", **kw)


def test_reusing_the_textbook_where_it_is_clear_is_not_a_problem():
    """Whether a passage needed explaining is a judgement about meaning: the program never counts copied text. (A
    copy check once forced rewording, review B called the rewording a contradiction, and the handout swung back and
    forth between the two until it ended as the textbook again.)"""
    assert gates.handout_program(task(ZH), ZH + "\n\n換句話說，NLP 要掌握的是意思，不只是字面。") == []
    assert gates.handout_program(task(EN), EN) == []


def test_the_textbooks_equations_must_be_written_but_a_lone_symbol_need_not():
    t = task("價值函數定義為 $$v_\\pi(s) \\doteq \\mathbb{E}_\\pi[G_t \\mid S_t = s]$$ 其中 $G_t$ 是回報。")
    fs = gates.handout_program(t, "（講義）價值函數衡量從狀態出發的期望回報。")
    assert [f.code for f in fs] == ["formula_missing"] and fs[0].suggested_fix.startswith("$$")
    ok = "（講義）價值函數是 $$v_\\pi(s) \\doteq \\mathbb{E}_\\pi[G_t \\mid S_t = s]$$ 也就是期望回報。"
    assert gates.handout_program(t, ok) == []
    ev = {"status": "confirmed", "transcription": {"formulas": ["S_t", "p(s', r \\mid s, a) = \\Pr\\{S_t = s'\\}"], "symbols": []}}
    assert gates.required_formulas(task("文字層"), ev) == ["p(s',r\\mids,a)=\\Pr\\{S_t=s'\\}"]


def test_a_reviewers_claim_counts_only_with_quotations_that_are_really_there():
    """Review B must quote both the handout and the textbook; review A's "yes" must quote the handout. What does
    not check out is a note, never a reason to correct or hold."""
    guide = "NLP 的目標是讓機器理解、解釋並生成人類語言。它已經從語言分析走到語意理解。"
    real = ConflictsReview(conflicts=[Conflict(guide_quote="它已經從語言分析走到語意理解",
                                               source_quote="從簡單的語言分析進階到複雜的語意理解", why="（測試）",
                                               both_true="no")])
    fs = gates.conflict_findings(real, guide, ZH)
    assert [(f.code, f.severity, f.status) for f in fs] == [("conflict", "major", "confirmed")]
    invented = ConflictsReview(conflicts=[Conflict(guide_quote="NLP 只能找關鍵字", source_quote="課本沒有這句", why="",
                                                   both_true="no")])
    fs = gates.conflict_findings(invented, guide, ZH)
    assert [(f.code, f.severity) for f in fs] == [("conflict_unconfirmed", "minor")]
    assert gates.reader_notes(fs)[0]["code"] == "conflict_unconfirmed"
    reworded = ConflictsReview(conflicts=[Conflict(guide_quote="它已經從語言分析走到語意理解",
                                                   source_quote="從簡單的語言分析進階到複雜的語意理解",
                                                   why="用字不同", both_true="yes")])
    assert gates.conflict_findings(reworded, guide, ZH) == []          # both can be true: not a contradiction
    pts = [("P1", "NLP 的目標"), ("P2", "從分析到理解")]
    a = PointsReview(points=[PointVerdict(id="P1", taught="yes", guide_quote="讓機器理解、解釋並生成人類語言"),
                             PointVerdict(id="P2", taught="no")])
    fs = gates.points_findings(pts, a, guide)
    assert [(f.code, f.item, f.severity) for f in fs] == [("point_missing", "P2", "major")]
    a = PointsReview(points=[PointVerdict(id="P1", taught="yes", guide_quote="講義裡沒有的句子"), PointVerdict(id="P2", taught="yes",
                                                                                               guide_quote="從語言分析走到語意理解")])
    assert [(f.code, f.status) for f in gates.points_findings(pts, a, guide)] == [("point_unconfirmed", "unconfirmed")]


def test_a_correction_changes_only_the_passages_it_names():
    guide = "第一段講定義。\n\n第二段說它從語言分析走到語意理解，這是錯的說法。\n\n第三段舉例。"
    new, applied, skipped = gates.apply_edits(guide, [
        Edit(problem="F1", action="replace", old="這是錯 的說法", new="這正是 NLP 的發展方向"),     # spacing ignored
        Edit(problem="F2", action="insert_after", old="第一段講定義。", new="補上：NLP 的三個目標。"),
        Edit(problem="F3", action="replace", old="講義裡沒有的句子", new="不會套用"),
    ])
    assert new == "第一段講定義。\n\n補上：NLP 的三個目標。\n\n第二段說它從語言分析走到語意理解，這正是 NLP 的發展方向。\n\n第三段舉例。"
    assert [a["problem"] for a in applied] == ["F1", "F2"] and skipped == ["F3：要換掉的句子不在講義中"]
    end, _, _ = gates.apply_edits("只有一段。", [Edit(action="insert_after", old="", new="補在最後。")])
    assert end == "只有一段。\n\n補在最後。"


def test_the_points_are_the_blocks_summary():
    t = task("x", topic={"index": 1, "summary": "主題總覽", "chunks": [
        {"topic": "NLP", "aspect": "定義", "summary": "第一塊"}, {"topic": "NLP", "aspect": "發展", "summary": "從分析到理解"}]})
    assert gates.handout_points(t) == [("P1", "（NLP｜發展）從分析到理解")]
    assert gates.handout_points(task("x", topic={"summary": "只有總覽"})) == [("P1", "只有總覽")]
    assert gates.handout_points(task("x")) == []


def test_markers_ignore_code_and_treat_only_if_not_only_as_a_condition():
    from app.reading.markers import parity, profile
    code = "```python\nresult = [n**2 for n in range(10) if n % 2 == 0]\n```"
    assert "condition" not in profile(code)                          # `if` inside code is not a condition
    assert parity("This paragraph explains the idea in words only.", "這段只用文字說明。") == []
    assert parity("Only if x > 0 is the formula valid.", "這個公式有效。")    # "only if" still is a condition


def test_a_merged_quotation_counts_only_when_every_piece_is_in_the_guide():
    """Auditors often join two guide sentences into one quotation with an added 「且」; each piece must still be verbatim."""
    guide = "在均衡價格下，消費者想買的數量恰好等於生產者想賣的數量。兩條曲線的交點稱為市場均衡點。"
    merged = "在均衡價格下，消費者想買的數量恰好等於生產者想賣的數量，且兩條曲線的交點稱為市場均衡點。"
    full = ra.expand_quote(guide, merged)
    assert "在均衡價格下" in full and "市場均衡點" in full
    invented = "消費者想買的數量恰好等於生產者想賣的數量；價格會立刻回到均衡"
    assert ra.expand_quote(guide, invented) == ""                      # one piece is not in the guide
    hidden = "在均衡價格下，不，消費者想買的數量恰好等於生產者想賣的數量"
    assert ra.expand_quote(guide, hidden) == ""                        # a short inserted piece is checked too
