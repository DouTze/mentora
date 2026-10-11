"""Program rules of the handout review (reading-graph-v3), each from a case seen with qwen3-vl:4b: what the program
treats as a fact (a reprint of the textbook, a missing equation, a reviewer's quotation that is not there) and what
it leaves to the reviewers."""
from app.reading import agent as ra
from app.reading import gates
from app.reading.contracts import Edit, ParagraphCheck, ParagraphsReview, PointsReview, PointVerdict

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
    paras = gates.review_paragraphs(guide)
    real = ParagraphsReview(paragraphs=[ParagraphCheck(id="P1", guide_says="已從語言分析走到語意理解",
                                                       source_quote="從簡單的語言分析進階到複雜的語意理解", relation="conflict")])
    fs = gates.paragraph_findings([real], paras, ZH)
    assert [f.code for f in fs] == ["conflict"] and fs[0].draft_excerpt == paras[0]
    invented = ParagraphsReview(paragraphs=[ParagraphCheck(id="P1", guide_says="NLP 只能找關鍵字", source_quote="課本沒有這句",
                                                           relation="conflict")])
    assert [f.code for f in gates.paragraph_findings([invented], paras, ZH)] == ["conflict_unconfirmed"]
    agreed = ParagraphsReview(paragraphs=[ParagraphCheck(id="P1", source_quote="從簡單的語言分析進階到複雜的語意理解",
                                                         relation="consistent")])
    assert gates.paragraph_findings([agreed], paras, ZH) == []          # the same claim in other words
    assert [f.code for f in gates.paragraph_findings([agreed, real], paras, ZH)] == ["conflict"]   # either reading
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


def test_a_handout_slipping_into_another_script_is_a_program_fact():
    """qwen3-vl:4b wrote "a mapping from state-action pairs to next状态" in an English handout. Characters of a script
    the source does not use are a slip in source mode; in a 繁體中文 handout English terms are normal."""
    slipped = "The transition function is a mapping from state-action pairs to next状态. It is a probability."
    fs = gates.handout_program(task(EN, language="src"), slipped)
    assert [(f.code, f.draft_excerpt) for f in fs] == [("language_mixed", "The transition function is a mapping from "
                                                                          "state-action pairs to next状态.")]
    assert gates.handout_program(task(EN, language="zh"), "轉移函數 (transition function) 把狀態對應到下一個狀態。") == []
    assert gates.handout_program(task(ZH, language="src"), "NLP（自然語言處理）讓機器理解語言。") == []


def test_the_handout_bound_is_converted_to_characters_by_the_sources_language():
    """A bound of 80 % of the token budget counted in characters cut English handouts at 1638 characters (about 400
    tokens): an English character is a third of a token or less."""
    en, zh = EN * 3, ZH * 3
    assert gates.handout_chars(en) > 2.5 * gates.handout_chars(zh)
    assert gates.handout_schema(en)["properties"]["guide"]["maxLength"] == gates.handout_chars(en)


def test_another_answer_field_written_into_the_handout_is_a_program_fact():
    """qwen3-vl:4b once wrote `key_points: ["…"]` at the end of the handout text and left key_points empty."""
    g = "MDPs combine evaluative feedback with an associative aspect.\n\nkey_points: [\"MDPs formalize decisions.\"]"
    fs = gates.handout_program(task(EN, language="src"), g)
    assert [(f.code, f.draft_excerpt) for f in fs] == [("field_leak", 'key_points: ["MDPs formalize decisions."]')]
    assert gates.handout_program(task(EN, language="src"), "Quick check: can you name the two parts of an MDP?") == []


def test_every_bound_of_a_handout_answer_follows_the_sources_language():
    """key_points and check were still bounded in CJK characters: an English key point stopped at "…and optimal"."""
    en, zh = gates.handout_schema(EN * 3)["properties"], gates.handout_schema(ZH * 3)["properties"]
    assert en["key_points"]["items"]["maxLength"] > 2 * zh["key_points"]["items"]["maxLength"]
    assert en["check"]["maxLength"] > 2 * zh["check"]["maxLength"]


def test_the_program_tidies_what_it_sees_exactly_instead_of_spending_corrections_on_it():
    """A leaked answer field, a slipped copy of a textbook formula and a paragraph written over again are corrected
    by the program: a small model asked to correct them gets them wrong again and again."""
    source = "We compute the expected reward:\n\n$$r(s,a)\\doteq\\mathbb{E}[R_{t}|S_{t-1}=s]. \\tag{3.5}$$"
    slipped = "$$r(s,a)\\buteq\\bbE[R_t|S_{t-1}=s].\\tag{3.5}$$"
    guide = ("The expected reward averages what follows a state.\n\n" + slipped + "\n\nHere $s$ is the state.\n\n"
             + slipped + "\n\nHere $s$ is the state.\n\n"
             + 'Rewards matter. key_points": ["Rewards"], "check": "Why?"}')
    out = gates.tidy_handout(guide, source)
    assert out.count("$$") == 2 and "\\doteq\\mathbb{E}[R_{t}|S_{t-1}=s]" in out and "\\buteq" not in out
    assert out.count("Here $s$ is the state.") == 1 and "key_points" not in out and out.endswith("Rewards matter.")
    own = "$$r(s,a)=\\sum_{r}r\\,p(r|s,a)$$"                 # the handout's own formula, not a copy, stays
    assert gates.tidy_handout(own, source).startswith(own + "\n\n$$r(s,a)\\doteq")   # the textbook's written after it


def test_an_edit_replacing_text_over_several_paragraphs_is_not_applied():
    """A correction changes the sentences a problem is about; a passage over several paragraphs replaced is the
    handout written anew (a small model copied most of it and put the textbook in its place)."""
    guide = "First paragraph explains states.\n\nSecond paragraph explains actions.\n\nThird explains rewards."
    new, applied, skipped = gates.apply_edits(guide, [Edit(problem="F1", action="replace",
                                                           old="First paragraph explains states.\n\nSecond paragraph explains actions.",
                                                           new="The textbook's last paragraph.")])
    assert new == guide and not applied and "跨了好幾段" in skipped[0]
    new, applied, _ = gates.apply_edits(guide, [Edit(problem="F1", action="replace", old="Second paragraph explains actions.",
                                                     new="Second paragraph explains what an action is.")])
    assert "what an action is" in new and applied



def test_a_transcribed_formula_the_passage_does_not_have_is_dropped():
    """A small model reading an image sometimes answers with the prompt's example: a formula none of whose letters
    are in the passage is not the passage's and is never required from the handout."""
    from app.reading.contracts import SymbolTranscription
    tr = SymbolTranscription(formulas=[r"\mathbf{w} \leftarrow \mathbf{w} - \eta \nabla L", r"q_{\ast}(s,a)"],
                             symbols=[{"symbol": "w", "latex": r"\mathbf{w}", "style": "bold", "script": "none"},
                                      {"symbol": "s", "latex": "s", "style": "italic", "script": "none"}],
                             unreadable=[])
    a = gates.anchored(tr, r"In MDPs we estimate the value $q_{\ast}(s,a)$ of each action $a$ in each state $s$.")
    assert a.formulas == [r"q_{\ast}(s,a)"] and [x.symbol for x in a.symbols] == ["s"]
    b = gates.anchored(tr, r"The weight vector $\mathbf{w}$ moves by $\eta\nabla L$ each step.")
    assert b.formulas == tr.formulas[:1]


def test_a_formula_is_matched_by_its_equation_number_and_without_sentence_punctuation():
    """A copy numbered (3.2) is the textbook's (3.2), however garbled; the period or comma a display formula closes
    its sentence with is not part of the formula; an end mark (□) is not a formula to teach."""
    source = "The dynamics:\n\n$$p(s',r|s,a)\\doteq\\Pr\\{S_t=s'\\},\\tag{3.2}$$\n\nDone. $$\\square$$"
    garbled = "Write it.\n\n$$p(s',r|s,a)\\bapprox\\text{Pr}\\big\\backslash{S_t=s'\\tag{3.2}$$"
    assert "\\doteq\\Pr\\{S_t=s'\\},\\tag{3.2}" in gates.tidy_handout(garbled, source)
    assert gates.tidy_handout("$$x=1\\tag{9.9}$$", source).startswith("$$x=1\\tag{9.9}$$")   # not the passage's
    t = ra.ReadingTaskInput.model_construct(text=source, language="src", block_ids=[])
    inline = "The dynamics are $p(s',r|s,a)\\doteq\\Pr\\{S_t=s'\\}\\tag{3.2}$ for every state."
    assert [f.code for f in gates.handout_program(t, inline)] == []



def test_a_textbook_formula_the_handout_writes_in_part_is_written_in_full_by_the_program():
    """A small model writes a long formula in part (its right-hand side); asked to write it in full it adds copies and
    explanations each time. The program writes the textbook's formula after the paragraph that talks about it."""
    source = "The return is finite:\n\n$$G_{t}=\\sum_{k=0}^{\\infty}\\gamma^{k}=\\frac{1}{1-\\gamma}. \\tag{3.10}$$"
    guide = "Rewards are discounted.\n\nThe sum $\\sum_{k=0}^\\infty \\gamma^k = \\frac{1}{1-\\gamma}$ is finite."
    out = gates.tidy_handout(guide, source)
    assert out.split("\n\n")[2] == "$$G_{t}=\\sum_{k=0}^{\\infty}\\gamma^{k}=\\frac{1}{1-\\gamma}. \\tag{3.10}$$"
    written = "Rewards are discounted: $G_t=\\sum_{k=0}^{\\infty}\\gamma^k=\\frac{1}{1-\\gamma}$."   # in full, unnumbered
    assert gates.tidy_handout(written, source) == written


def test_a_field_label_opening_a_paragraph_and_tripled_dollars_are_tidied():
    """Other fields written as plain labels ("Key points:", "Check:") open a paragraph that is not the handout's; a
    formula set in $$$ … $$$ does not render."""
    guide = ("The robot has two states.\n\nThe state set is $$$\\mathcal{S}=\\{a,b\\}$$$ here.\n\n"
             "Key points: the robot balances risk.\n\nCheck: Why recharge?")
    out = gates.tidy_handout(guide, "The robot has two states.")
    assert out == "The robot has two states.\n\nThe state set is $$\\mathcal{S}=\\{a,b\\}$$ here."
    kept = "You can check: the sum is one."                   # a word inside a sentence is no label
    assert gates.tidy_handout(kept, "x") == kept


def test_a_formula_named_by_its_number_elsewhere_is_shown_with_the_passage():
    """An exercise "prove the second equality in (3.10)" without (3.10) in view made a small model invent the formula:
    the display formula set elsewhere in the reading comes with the passage, for reference only (it is not one the
    handout has to write)."""
    from app.reading import agent as ra
    eq = "$$G_{t}=\\sum_{k=0}^{\\infty}\\gamma^{k}=\\frac{1}{1-\\gamma}. \\tag{3.10}$$"
    other = "If the reward is a constant +1, then the return is\n\n" + eq
    exercise = "Exercise 3.10 Prove the second equality in (3.10). $$\\square$$"
    assert ra.cited_formulas(exercise, [other, "No formulas here (3.10)."]) == [eq]
    assert ra.cited_formulas(other, [exercise]) == []                     # its own formula is no citation
    t = ra.ReadingTaskInput.model_construct(text=exercise, cited=[eq], has_figure=False, heading=None, material_title="m",
                                            range_label="r", index=0, total=1, label="l", unit_id="u", prev_points=[],
                                            qa_context=[])
    assert eq in ra.task_fields(t)["quote_text"]
    assert gates.required_formulas(t, None) == []


def test_a_greek_or_logic_sign_the_passage_never_uses_is_corrected():
    """γ written as \\forall (or as β) in the handout's math is a symbol the passage never uses: a program finding
    naming the passage's own; a sign the passage before it or a cited formula uses is the textbook's."""
    from app.reading import agent as ra
    src = "Exercise 3.9 Suppose $\\gamma=0.9$ and the reward sequence is $R_{1}=2$ followed by 7s."
    t = ra.ReadingTaskInput.model_construct(text=src, cited=[], topic=None, prev_points=[], language="src")
    bad = "The discount factor is $\\forall=0.9$, so $G_1 = R_1 + \\forall G_2$."
    (f,) = [x for x in gates.handout_program(t, bad) if x.code == "symbol_foreign"]
    assert "\\forall" in f.detail and "\\gamma" in f.detail
    assert not [x for x in gates.handout_program(t, "With $\\gamma=0.9$, $G_1 = R_1 + \\gamma G_2$.")
                if x.code == "symbol_foreign"]
    t2 = ra.ReadingTaskInput.model_construct(text="Exercise 3.6 What would the return be at each time?", cited=[],
                                             topic={"prev": {"text": "the return is related to $-\\gamma^{K}$"}},
                                             prev_points=[], language="src")
    assert not [x for x in gates.handout_program(t2, "The return is $-\\gamma^{K}$.") if x.code == "symbol_foreign"]


def test_a_sentence_said_twice_a_letter_style_or_an_operator_as_a_word_is_corrected():
    """A sentence repeated nearly word for word, 𝒮 written \\mathcal when the passage sets it \\mathscr, and \\sum
    written \\text{sum} are program findings; a sentence that only touches the same idea is not."""
    from app.reading import agent as ra
    src = "The four-argument function $p$ gives $p(s'|s,a)=\\sum_{r\\in\\mathscr{R}}p(s',r|s,a)$ for every state."
    t = ra.ReadingTaskInput.model_construct(text=src, cited=[], topic=None, prev_points=[], language="src")
    said = "The state-transition probability $p(s'|s,a)$ is the probability of moving to state $s'$ after action $a$."
    g = (f"{said} It is computed from the four-argument function.\n\n{said.replace('moving', 'going')}\n\n"
         "In symbols, $p(s'|s,a)=\\text{sum}_{r\\in\\mathcal{R}}p(s',r|s,a)$.")
    codes = {f.code: f for f in gates.handout_program(t, g)}
    assert {"repeated_text", "symbol_foreign", "math_word"} <= set(codes)
    assert "\\mathscr" in codes["symbol_foreign"].detail and "\\sum" in codes["math_word"].detail
    ok = (f"{said}\n\nSumming the four-argument function over the rewards gives it: "
          "$p(s'|s,a)=\\sum_{r\\in\\mathscr{R}}p(s',r|s,a)$.")
    assert not [f for f in gates.handout_program(t, ok) if f.code in ("repeated_text", "symbol_foreign", "math_word")]


def test_a_formula_the_region_shows_in_part_is_the_passages_own_not_another_to_write():
    """The vision reading of a region gave half of (3.4) ("p(s'|s,a) ≐ Pr{…} =") and the rows of an aligned display
    one by one: required as formulas of their own, the handout could never write them and its corrections went in
    circles. A reading the passage's formulas already contain is not required again; one the text lost still is."""
    from app.reading import agent as ra
    src = ("such as the state-transition probabilities\n\n"
           "$$p(s'|s,a)\\doteq\\Pr\\{S_{t}=s'|S_{t-1}=s,A_{t-1}=a\\}=\\sum_{r\\in\\mathscr{R}}p(s',r|s,a). \\tag{3.4}$$\n\n"
           "Returns are related:\n\n$$\\begin{aligned}G_{t}&\\doteq R_{t+1}+\\gamma R_{t+2}+\\cdots \\\\ &=R_{t+1}+\\gamma G_{t+1}"
           "\\end{aligned} \\tag{3.9}$$\n\nwith $r_{\\text{search}}>r_{\\text{wait}}$.\n\n"
           "$$G_{t}=\\sum_{k=0}^{\\infty}\\gamma^{k}=\\frac{1}{1-\\gamma}. \\tag{3.10}$$")
    t = ra.ReadingTaskInput.model_construct(text=src)
    ev = {"status": "confirmed", "transcription": {"formulas": [
        "$p(s'|s,a)\\doteq\\Pr\\{S_t=s'|S_{t-1}=s,A_{t-1}=a\\}=$", "$G_t=R_{t+1}+\\gamma G_{t+1}$",
        "r_{\\text{search}}>r_{\\text{wait}}", "$G_t=\\frac{1}{1-\\text{\\gamma}}$",
        "\\mathbf{w}\\leftarrow\\mathbf{w}-\\eta\\nabla L"]}}
    extra = [f for f in gates.required_formulas(t, ev) if f not in ra.source_formulas(src)[0]]
    assert extra == [ra._norm_math("\\mathbf{w}\\leftarrow\\mathbf{w}-\\eta\\nabla L")]
