"""Source blocks and unit planning (P2): text is kept by the program, plans are validated, rules are a valid
fallback, buffers never split protected relations, and old databases migrate in place."""
import json
import sqlite3

import pytest

from app.db import Database
from app.llm.base import LLMBackend, LLMError, LLMResult
from app.llm.mock import MockBackend
from app.llm.runtime import BackendPool, CallBudget, ListSink, ModelRuntime, ModelSnapshot
from app.llm.tasks import TaskContext
from app.persistence.repository import Repository
from app.reading import segmentation as seg
from app.reading.prompts import load_bundle
from app.reading.source import MAX_BLOCK_CHARS, SourceError, extract_blocks
from evals import reading_eval as ev


@pytest.fixture(scope="module")
def manifest():
    return ev.load_manifest()


def blocks_of(manifest, cid):
    case = ev.select_cases(manifest, [cid])[0]
    unit, units = ev.load_units(case)
    return case, unit, units, extract_blocks(cid, unit, 1, len(units), units)[1]


class Scripted(LLMBackend):
    name, model = "scripted", "scripted"

    def __init__(self, outputs):
        self.outputs, self.requests = list(outputs), []

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
        self.requests.append({"messages": messages, "meta": meta})
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResult(text=out if isinstance(out, str) else json.dumps(out), latency_ms=1)


def deps_for(backend, outline: bool = False):
    """Planner deps over the current prompt bundle. Without `outline` the bundle lacks segment_outline, like a
    session pinned to a snapshot from before units-v9: the buffer planner (segment_blocks …) is what is tested."""
    rt = ModelRuntime(ModelSnapshot("mock", "", "", "", "", 0.2, 512, 8192), BackendPool(build=lambda c: backend))
    sink = ListSink()
    b = load_bundle()
    prompts = b["prompts"] if outline else {k: v for k, v in b["prompts"].items() if k != "segment_outline"}
    return seg.PlannerDeps(make_ctx=lambda task, key: TaskContext(rt, CallBudget.for_task(task), sink),
                           prompts=prompts, prompt_version=b["hash"], material_title="t"), sink


# -- blocks ---------------------------------------------------------------------------

def test_every_fixture_rebuilds_exactly_from_its_blocks(manifest):
    for case in ev.select_cases(manifest, include_broken=False):
        unit, units = ev.load_units(case)
        _, blocks = extract_blocks(case["id"], unit, 1, len(units), units)
        body, heads = ev.split_body(unit, units)
        assert ev.norm("".join(b.text for b in blocks if b.kind != "heading")) == body, case["id"]
        assert [ev.norm(b.text) for b in blocks if b.kind == "heading"] == heads, case["id"]
        assert len({b.block_id for b in blocks}) == len(blocks)
        assert [b.ordinal for b in blocks] == list(range(len(blocks)))


def test_locators_point_back_into_the_extracted_text(manifest):
    _, unit, units, blocks = blocks_of(manifest, "cross-page-condition")
    for b in blocks:
        first, last = units[b.locator.start - 1], units[b.locator.end - 1]
        if b.locator.start == b.locator.end:
            assert ev.norm(first[b.locator.char_start:b.locator.char_end]) == ev.norm(b.text)
        else:   # a sentence across a page break: start page from char_start, end page up to char_end
            assert ev.norm(first[b.locator.char_start:] + last[:b.locator.char_end]) == ev.norm(b.text)
    _, unit, units, blocks = blocks_of(manifest, "list-tcp")
    for b in blocks:
        lines = units[b.locator.start - 1:b.locator.end]
        assert ev.norm("\n".join(lines)).endswith(ev.norm(b.text)) or b.kind == "heading"


def test_code_fence_with_blank_lines_and_hash_comment_stays_one_block(manifest):
    _, _, _, blocks = blocks_of(manifest, "code-block")
    code = [b for b in blocks if b.kind == "code"]
    assert len(code) == 1 and "# 等價的迴圈寫法" in code[0].text and "result.append(n * n)" in code[0].text
    assert [b.kind for b in blocks if b.kind == "heading"] == ["heading"]


def test_identical_paragraphs_get_distinct_ids_and_snapshot_is_content_addressed(manifest):
    _, unit, units, blocks = blocks_of(manifest, "duplicate-paragraphs")
    dup = [b for b in blocks if b.text.startswith("重點回顧")]
    assert len(dup) == 2 and dup[0].text_hash == dup[1].text_hash and dup[0].block_id != dup[1].block_id
    s1, _ = extract_blocks("m", unit, 1, len(units), units)
    s2, _ = extract_blocks("m", unit, 1, len(units), units)
    s3, _ = extract_blocks("m", unit, 1, len(units), units[:-1])
    assert s1["snapshot_id"] == s2["snapshot_id"] != s3["snapshot_id"]


def test_page_break_mid_sentence_stays_one_sentence(manifest):
    _, _, _, blocks = blocks_of(manifest, "cross-page-condition")
    b = next(b for b in blocks if b.locator.start != b.locator.end)
    assert (b.locator.start, b.locator.end) == (1, 2) and "連續；第二，函數在開區間" in b.text
    assert b.meta["pdf_paras"] == [[1, 3], [2, 1]]


def test_pdf_table_cells_are_a_table_and_a_numbered_heading_after_a_table_is_found_by_its_siblings():
    """Word export of a textbook (科目1 3.1): short table cells look like headings, and "A. 第一階段" follows the
    table's unfinished last cell, so the plain heading guess calls the cells headings and misses "A." (B. is found)."""
    pages = ["\n\n".join([
        "（2）技術演進脈絡",
        "自然語言處理（NLP）的發展歷程呈現出明確的技術世代演進脈絡，從早期的規則",
        "式系統、統計語言模型，再到進入深度學習時代，最後迎來預訓練語言模型的主",
        "導潮流。每個技術階段的突破不僅提高了語言處理能力，也改變了模型設計的思維。",
        "以下整理NLP 技術演進階段概覽：",
        "時期技術階段核心特徵代表技術/模型",
        "1980s–1990s規則式方法",
        "（Rule-based）",
        "手動設計語法規則與詞彙辭典ELIZA、專家系統",
        "1990s–2010統計語言模型",
        "（Statistical LM）",
        "使用大量語料訓練機率模型N-gram、HMM、CRF",
        "BERT、GPT、RoBERTa、",
        "T5",
    ]), "\n\n".join([
        "A. 第一階段：規則式方法（1980s–1990s）",
        "• 階段描述/優勢：在這一階段，NLP 主要依賴語言學家與工程師手工編寫的語法",
        "規則與詞彙辭典進行語言處理，這一方法的優勢在於其高可解釋性與穩定的結果。",
        "B. 第二階段：統計語言模型（1990s–2010s）",
        "• 階段描述/優勢：為突破規則式方法的限制，NLP 進入了以機率為基礎的統計建",
        "模時期，研究者開始依靠大量語料來訓練機率模型，大幅提高了處理的靈活性。",
    ])]
    _, blocks = extract_blocks("m", "page", 1, 2, pages)
    assert [b.text for b in blocks if b.kind == "heading"] == [
        "（2）技術演進脈絡", "A. 第一階段：規則式方法（1980s–1990s）", "B. 第二階段：統計語言模型（1990s–2010s）"]
    tables = [b for b in blocks if b.kind == "table"]
    assert len(tables) == 1 and tables[0].text.split("\n")[0] == "時期技術階段核心特徵代表技術/模型"
    assert tables[0].text.endswith("BERT、GPT、RoBERTa、\nT5")              # the wrapped last cell stays in the table
    assert tables[0].meta["pdf_paras"] == [[1, k] for k in range(6, 15)]
    assert (tables[0].locator.char_start, tables[0].locator.char_end) == (pages[0].index("時期"), len(pages[0]))
    intro = next(b for b in blocks if b.text == "以下整理NLP 技術演進階段概覽：")
    assert blocks[blocks.index(intro) + 1] is tables[0]
    assert seg.glues(blocks)[blocks.index(intro)]                            # the table stays with its lead-in


def test_one_line_sentences_inside_a_table_region_are_cells():
    """A table whose description cells are whole sentences: the region runs through them, and its last row keeps as
    many sentence cells as the rows before it; the numbered item after the table is not part of it."""
    rows = [("分詞", "（Tokenization）", "將一段文字拆解為單字、詞組或子詞。", "基於空白或斷詞字典。"),
            ("詞性標注", "（Part-of-Speech）", "標記每個詞的語法角色。", "常用於語法分析。"),
            ("停用詞移除", "（Stopword Removal）", "去除語意貢獻低的高頻詞。", "可降低資料維度。")]
    page = "\n\n".join(["以下是NLP 前處理常見技術，在實務應用中，依任務需求、這些步驟常以不同順序或深度搭配執行。",
                        "處理步驟說明常見工具與技巧"] + [c for r in rows for c in r] + [
                        "（1）分詞（Tokenization）：",
                        "文字處理的起點，其精準與否直接影響後續每個處理階段，針對中文、日文等無空白語言尤其重要。"])
    _, blocks = extract_blocks("m", "page", 1, 1, [page])
    tables = [b for b in blocks if b.kind == "table"]
    assert len(tables) == 1 and tables[0].text.split("\n") == ["處理步驟說明常見工具與技巧"] + [c for r in rows for c in r]
    assert not [b for b in blocks if b.kind == "heading"]
    assert blocks[-1].text.startswith("（1）分詞") or blocks[-2].text.startswith("（1）分詞")


def test_word_style_pdf_lines_are_rebuilt_into_sentences_and_paragraphs():
    """A Word export with wide line spacing: every line is its own layout block. Mid-sentence lines (short, no closing
    punctuation) are not headings; sentences are rebuilt across lines and grouped by paragraph."""
    page = "\n\n".join([
        "1. 前言與章節導覽",
        "隨著人工智慧（AI）技術的迅速發展，自然語言處理（Natural Language",
        "Processing, NLP）已逐漸成為AI 生態系統的核心之一。NLP 技術賦予機器理解、",
        "處理並生成自然語言的能力，成為連接人類與機器溝通的重要橋樑。",
        "本節內容將聚焦於自然語言處理技術的基礎概念與實務應用，介紹NLP 在現代",
        "AI 應用中的關鍵角色。",
        "A.",
        "理解（Understand）：從語言中解析出語法結構與語意。",
        "• 自然語言生成（NLG）：專注於機器如何產生自然語言。",
    ])
    _, blocks = extract_blocks("m", "page", 1, 1, [page])
    assert [b.text for b in blocks if b.kind == "heading"] == ["1. 前言與章節導覽"]
    body = [b for b in blocks if b.kind != "heading"]
    assert [b.text for b in body] == [
        "隨著人工智慧（AI）技術的迅速發展，自然語言處理（Natural Language Processing, NLP）已逐漸成為AI 生態系統的核心之一。",
        "NLP 技術賦予機器理解、處理並生成自然語言的能力，成為連接人類與機器溝通的重要橋樑。",
        "本節內容將聚焦於自然語言處理技術的基礎概念與實務應用，介紹NLP 在現代AI 應用中的關鍵角色。",
        "A. 理解（Understand）：從語言中解析出語法結構與語意。",
        "• 自然語言生成（NLG）：專注於機器如何產生自然語言。"]
    groups = [b.meta["para_group"] for b in body]
    assert groups[0] == groups[1] and len(set(groups)) == 4
    assert [(b.locator.para, b.locator.paras) for b in body][:3] == [(2, 5), (2, 5), (3, 5)]
    units = seg.rules_groups(blocks)
    assert seg.validate_groups(blocks, units) == [] and len(units[0]) == 3   # heading + its paragraph
    assert seg.unit_text([blocks[i] for i in units[0]]) == body[0].text + body[1].text
    listing, meta = seg.window_listing(blocks, seg.make_window(blocks, 0, 0, len(blocks)))
    assert "【B3】（句子，接續上一句的段落）" in listing and "【B2】（句子，程式猜測的段落開頭）" in listing
    assert [x["same_para"] for x in meta["blocks"]][:4] == [False, False, True, False]


def test_oversize_paragraph_is_cut_at_sentence_ends_outside_formulas():
    para = ("第一句說明條件，" * 20 + "。") * 3 + "其中 $a. b$ 不能拆。" + ("第三句補充結論，" * 30 + "。") * 2
    _, blocks = extract_blocks("m", "line", 1, 1, [para])
    assert len(blocks) > 1 and all(len(b.text) <= MAX_BLOCK_CHARS for b in blocks)
    assert {b.parent_block_id for b in blocks} == {blocks[0].parent_block_id} and [b.piece for b in blocks] == list(range(1, len(blocks) + 1))
    assert "".join(b.text for b in blocks) == para.replace(" ", "") or ev.norm("".join(b.text for b in blocks)) == ev.norm(para)
    assert all(b.text.count("$") % 2 == 0 for b in blocks)
    assert all(para[b.locator.char_start:b.locator.char_end] == b.text for b in blocks)


def test_formula_without_safe_boundary_is_refused_not_cut():
    lines = ["$$"] + ["x_{%d} + " % i * 30 for i in range(60)] + ["$$"]
    with pytest.raises(SourceError) as e:
        extract_blocks("m", "line", 1, len(lines), lines)
    assert e.value.code == "range_too_large"


# -- planning ---------------------------------------------------------------------------

def test_rules_plan_is_valid_and_respects_protected_relations_on_all_fixtures(manifest):
    for case in ev.select_cases(manifest, include_broken=False):
        unit, units = ev.load_units(case)
        _, blocks = extract_blocks(case["id"], unit, 1, len(units), units)
        groups = seg.rules_groups(blocks)
        assert seg.validate_groups(blocks, groups) == [], case["id"]
        rep = ev.check_segmentation(case, unit, units, [{"text": seg.unit_text([blocks[i] for i in g])} for g in groups
                                                         if seg.unit_text([blocks[i] for i in g])])
        assert rep.integrity_ok and not rep.protected_violations, (case["id"], rep.as_dict())


def test_formula_intro_and_where_clause_are_glued(manifest):
    _, _, _, blocks = blocks_of(manifest, "formula-where")
    g = seg.glues(blocks)
    f = next(i for i, b in enumerate(blocks) if b.kind == "formula")
    assert g[f - 1] and g[f]   # intro → formula → "其中 …"


def test_validate_groups_names_the_concrete_errors(manifest):
    _, _, _, blocks = blocks_of(manifest, "formula-where")
    f = next(i for i, b in enumerate(blocks) if b.kind == "formula")
    n = len(blocks)
    errs = seg.validate_groups(blocks, [list(range(0, f)), list(range(f, n))])
    assert any("不可切開" in e for e in errs)
    assert any("沒有用到" in e for e in seg.validate_groups(blocks, [list(range(0, n - 1))]))
    assert any("重複" in e for e in seg.validate_groups(blocks, [list(range(0, n)), [n - 1]]))


def test_buffers_never_cut_glued_pairs_and_carry_read_only_context(manifest):
    _, _, _, blocks = blocks_of(manifest, "list-tcp")
    g = seg.glues(blocks)
    for size in (30, 60, 100, 200):
        for lo in range(len(blocks)):
            hi = seg.buffer_end(blocks, lo, g, size)
            assert lo < hi <= len(blocks) and (hi == len(blocks) or not g[hi - 1])
            w = seg.make_window(blocks, 0, lo, hi)
            assert w.before == list(range(max(0, lo - seg.CONTEXT_BLOCKS), lo))
    assert seg.buffer_end(blocks, 0, [None] * len(blocks), 10 ** 6, window_tokens=1) == 1   # the token limit applies too


async def test_model_plan_returns_ids_and_text_is_rebuilt_by_the_program(manifest):
    case, unit, units, blocks = blocks_of(manifest, "zh-opportunity-cost")
    # B1 heading, B2 def, B3 example, B4 注意, B5 heading, B6 沉沒成本 — model also "rewrites" via a title only
    b = Scripted([{"units": [{"blocks": ["B1", "B2", "B3"], "reason": "定義與例子"}, {"blocks": ["B4"], "reason": "補充"},
                             {"blocks": ["B5", "B6"], "reason": "新標題"}]}])
    deps, sink = deps_for(b)
    plan = await seg.plan_units(blocks, deps)
    assert plan.planner == "model" and [len(u.block_ids) for u in plan.units] == [3, 1, 2]
    quotes = seg.to_quotes(unit, plan, {x.block_id: x for x in blocks})
    assert quotes[0]["heading"] == "機會成本" and quotes[0]["text"] == blocks[1].text + "\n\n" + blocks[2].text
    assert quotes[0]["boundary_reason"] == "定義與例子" and quotes[2]["heading"] == "沉沒成本"
    prompt = b.requests[0]["messages"][-2 if b.requests[0]["messages"][-1]["role"] == "assistant" else -1]["content"]
    assert "【B2】" in prompt and "不要重抄原文" in prompt
    rep = ev.check_segmentation(case, unit, units, quotes)
    assert rep.integrity_ok and rep.boundary_ok


def groups_of(plan, blocks):
    by_id = {x.block_id: i for i, x in enumerate(blocks)}
    return [[by_id[bid] for bid in u.block_ids] for u in plan.units]


async def test_out_of_order_plan_is_repaired_once_then_falls_back_to_rules(manifest):
    _, _, _, blocks = blocks_of(manifest, "formula-where")
    n = len(blocks)
    repeated = {"units": [{"blocks": [f"B1-B{n}"]}, {"blocks": [f"B{n}"]}]}
    b = Scripted([repeated, repeated])
    deps, sink = deps_for(b)
    plan = await seg.plan_units(blocks, deps)
    assert len(b.requests) == 2 and plan.planner == "rules" and plan.warnings
    assert "重複" in json.dumps(b.requests[1], ensure_ascii=False)
    assert seg.validate_groups(blocks, groups_of(plan, blocks)) == []


async def test_a_cut_through_a_glued_pair_or_a_left_out_block_is_fixed_without_discarding_the_plan(manifest):
    _, _, _, blocks = blocks_of(manifest, "formula-where")
    f = next(i for i, b in enumerate(blocks) if b.kind == "formula")
    n = len(blocks)
    # the model cuts between the formula's introduction and the formula, and leaves out the last block
    b = Scripted([{"units": [{"blocks": [f"B1-B{f}"], "reason": "引入"},
                             {"blocks": [f"B{f + 1}-B{n - 1}"], "reason": "之後"}]}])
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps)
    assert len(b.requests) == 1 and plan.planner == "model"
    groups = groups_of(plan, blocks)
    assert seg.validate_groups(blocks, groups) == [] and groups[0][-1] >= f + 1   # formula and `where` moved forward
    assert groups[-1][-1] == n - 1 and plan.units[0].boundary_reason == "引入"
    assert any("不可切開" in w for w in plan.warnings) and any("漏掉" in w for w in plan.warnings)


def long_topics(k=3, sents=28):
    """k topics, each a heading and one paragraph of ~sents * 27 chars; together well over the unit limit."""
    sent = "這一句說明主題的一個面向，內容足夠長以便測試長度上限。"
    lines = []
    for t in range(k):
        lines += [f"# 主題{t + 1}", sent * sents]
    return extract_blocks("m", "line", 1, len(lines), lines)[1]


async def test_an_oversize_unit_gets_a_focused_split_and_the_rest_of_the_plan_is_kept(manifest):
    blocks = long_topics()
    heads = [i for i, x in enumerate(blocks) if x.kind == "heading"]
    n = len(blocks)
    focused = {"units": [{"blocks": [f"B1-B{heads[1] - heads[0]}"], "reason": "主題一"},
                         {"blocks": [f"B{heads[1] - heads[0] + 1}-B{n - heads[0]}"], "reason": "主題二"}]}
    first = {"units": [{"blocks": [f"B1-B{heads[0]}"], "reason": "開頭"} if heads[0] else None,
                       {"blocks": [f"B{heads[0] + 1}-B{n}"], "reason": "全部主題"}]}
    first["units"] = [u for u in first["units"] if u]
    b = Scripted([first, focused])
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    assert len(b.requests) == 2 and b.requests[1]["meta"]["step"] == "segment_split"
    focus_prompt = b.requests[1]["messages"][0]["content"]
    assert "全部主題" in focus_prompt and "兩個以上" in focus_prompt
    groups = groups_of(plan, blocks)
    assert seg.validate_groups(blocks, groups) == []
    assert any(g[0] == heads[1] for g in groups)                # the model's topic turn is kept
    assert "主題一" in [u.boundary_reason for u in plan.units]


async def test_a_failed_focused_split_is_split_by_the_program_at_headings(manifest):
    blocks = long_topics()
    heads = [i for i, x in enumerate(blocks) if x.kind == "heading"]
    one = {"units": [{"blocks": [f"B1-B{len(blocks)}"], "reason": "全部"}]}
    b = Scripted([one, one, one])                               # window plan; focused split twice still one unit
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    assert len(b.requests) == 3 and plan.planner == "model"
    groups = groups_of(plan, blocks)
    assert seg.validate_groups(blocks, groups) == [] and len(groups) >= 2
    assert all(g[0] in heads for g in groups)                   # program cuts prefer headings
    assert any("程式依長度拆開" in w for w in plan.warnings)

    old = Scripted([one])                                       # a prompt snapshot without segment_split
    deps, _ = deps_for(old)
    deps.prompts = {k: v for k, v in deps.prompts.items() if k != "segment_split"}
    plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    assert len(old.requests) == 1 and seg.validate_groups(blocks, groups_of(plan, blocks)) == []


async def test_model_unavailable_propagates_instead_of_pretending(manifest):
    _, _, _, blocks = blocks_of(manifest, "headings")
    deps, _ = deps_for(Scripted([LLMError("down")]))
    from app.llm.tasks import TaskError
    with pytest.raises(TaskError) as e:
        await seg.plan_units(blocks, deps)
    assert e.value.kind == "unavailable"


async def test_finished_windows_are_reused_after_a_restart(manifest):
    _, _, _, blocks = blocks_of(manifest, "cross-window-argument")
    saved = {}
    deps, _ = deps_for(MockBackend())
    p1 = await seg.plan_units(blocks, deps, window_chars=150, on_window=lambda k, v: saved.__setitem__(k, v))
    assert len([k for k in saved if k.startswith("w")]) >= 2
    dead, _ = deps_for(Scripted([]))   # any model call would fail: everything must come from `saved`
    p2 = await seg.plan_units(blocks, dead, window_chars=150, done_windows=saved)
    assert [u.block_ids for u in p1.units] == [u.block_ids for u in p2.units]


async def test_the_last_unit_of_a_buffer_is_carried_into_the_next_one(manifest):
    """BUF: every unit but the last is emitted; the last one may continue, so its blocks open the next buffer."""
    _, _, _, blocks = blocks_of(manifest, "cross-window-argument")

    class OneUnitPerGluedRun(Scripted):
        """Groups each run of glued blocks (the smallest valid units)."""
        def __init__(self):
            super().__init__([])

        async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
            self.requests.append({"messages": messages, "meta": meta})
            units, cur = [], []
            for x in meta["blocks"]:
                cur.append(x["label"])
                if not x["glue_next"]:
                    units.append({"blocks": cur, "reason": "測試"})
                    cur = []
            if cur:
                units.append({"blocks": cur, "reason": "測試"})
            return LLMResult(text=json.dumps({"units": units}), latency_ms=1)

    b = OneUnitPerGluedRun()
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps, window_chars=150)
    assert plan.planner == "model" and len(b.requests) >= 2
    # a buffer with several units carries its last one (a single-unit buffer is emitted whole); the last buffer keeps all
    assert any("carried" in w for w in plan.windows) and "carried" not in plan.windows[-1]
    for w, nxt in zip(plan.windows, plan.windows[1:]):   # the carried unit opens the next buffer
        if "carried" in w:
            assert nxt["owned"][:len(w["carried"])] == w["carried"]
        else:
            assert nxt["owned"][0] not in w["owned"]
    assert [bid for u in plan.units for bid in u.block_ids] == [x.block_id for x in blocks]
    by_id = {x.block_id: i for i, x in enumerate(blocks)}
    assert seg.validate_groups(blocks, [[by_id[i] for i in u.block_ids] for u in plan.units]) == []


def test_a_unit_has_a_strict_size_limit_and_a_long_paragraph_may_split_between_sentences():
    sent = "這一句說明主題的一個面向，內容足夠長以便測試長度上限。"   # 27 chars
    para = sent * 80                                             # one paragraph of ~2160 chars, cut into pieces
    _, blocks = extract_blocks("m", "line", 1, 1, [para])
    n = len(blocks)
    assert n > 1 and not any(seg.glues(blocks))                  # pieces end at sentence ends: the planner may cut there
    errs = seg.validate_groups(blocks, [list(range(n))])
    assert any("太長" in e for e in errs)
    half = n // 2
    assert seg.validate_groups(blocks, [list(range(half)), list(range(half, n))]) == []
    rules = seg.rules_groups(blocks)                             # the fallback keeps the paragraph, within the limit
    assert seg.validate_groups(blocks, rules) == [] and len(rules) >= 2
    # a unit that cannot be split (one block, or all glued) is not refused for its size
    big = extract_blocks("m", "line", 1, 3, ["$$", "x + " * 900, "$$"])[1]
    assert seg.validate_groups(big, [[0]]) == []


async def test_a_heading_is_guidance_for_the_model_not_a_checked_rule(manifest):
    """A heading usually opens a new small topic, but the model may keep one inside a unit; its text then stays a line
    of the unit instead of disappearing."""
    case, unit, units, blocks = blocks_of(manifest, "zh-opportunity-cost")
    b = Scripted([{"units": [{"blocks": ["B1-B6"], "reason": "成本概念"}]}])
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps)
    assert plan.planner == "model" and len(plan.units) == 1
    q = seg.to_quotes(unit, plan, {x.block_id: x for x in blocks})[0]
    assert q["heading"] == "機會成本" and "\n\n沉沒成本\n\n" in q["text"]
    assert ev.check_segmentation(case, unit, units, [q]).integrity_ok


def test_unit_labels_accept_ranges():
    w = seg.Window(0, 10, 20)
    plan = seg.WindowPlan(units=[seg.PlannedUnit(blocks=["B1-B3"]), seg.PlannedUnit(blocks=["B4", "5–B10"])])
    assert seg.labels_to_groups(plan, w) == [[10, 11, 12], list(range(13, 20))]
    with pytest.raises(seg.PlanInvalid):
        seg.labels_to_groups(seg.WindowPlan(units=[seg.PlannedUnit(blocks=["B3-B11"])]), w)


def test_parts_carry_token_estimates(manifest):
    _, unit, _, blocks = blocks_of(manifest, "early-quiz")
    plan = seg.SegmentationPlan(units=[seg.SourceUnit(unit_id=f"u{i}", ordinal=i, block_ids=[x.block_id for x in g],
                                                      segmentation_version="t")
                                       for i, g in enumerate([[blocks[j] for j in grp] for grp in seg.rules_groups(blocks)])])
    quotes = seg.to_quotes(unit, plan, {x.block_id: x for x in blocks})
    parts, contracts = seg.plan_parts(quotes, part_chars=100)
    assert len(parts) > 1 and all(c.estimated_tokens > 0 and c.estimation_method == seg.TOKEN_METHOD for c in contracts)
    assert seg.estimate_tokens("中文字") == 4 and seg.estimate_tokens("abcdefghij") == 4


# -- persistence ------------------------------------------------------------------------

def test_old_database_is_migrated_in_place_and_old_rows_stay_legacy(tmp_path):
    path = tmp_path / "old.sqlite3"
    from app.db import SCHEMA
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,NULL)",
              ("old1", "t", "t", "active", json.dumps({"title": "x"}), json.dumps({"id": "rules-v1"}), "{}", "{}"))
    c.commit()
    c.close()
    db = Database(path)
    row = db.get_session("old1")
    assert row["workflow_runner"] is None and row["revision"] == 0 and row["workflow_meta"] is None
    Database(path)   # idempotent
    with db.conn() as c:
        assert [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version")][:2] == [1, 2]
    new = db.create_session({"title": "y"}, {"id": "rules-v1"}, {}, {}, workflow_runner="langgraph", workflow_meta={"v": 1})
    assert new["workflow_runner"] == "langgraph" and new["workflow_meta"] == {"v": 1}


def test_failed_migration_rolls_back(tmp_path):
    from app.db import migrate
    c = sqlite3.connect(tmp_path / "x.sqlite3", isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        migrate(c, [(2, "bad", "CREATE TABLE a (x INTEGER); CREATE TABLE a (y INTEGER)")])
    assert c.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None
    assert [r[0] for r in c.execute("SELECT version FROM schema_migrations")] == [1]


def test_snapshot_round_trip(tmp_path, manifest):
    db = Database(tmp_path / "db.sqlite3")
    repo = Repository(db)
    _, unit, units, blocks = blocks_of(manifest, "cross-page-condition")
    snap, _ = extract_blocks("cross-page-condition", unit, 1, len(units), units)
    repo.save_snapshot("mat", snap, blocks)
    repo.save_snapshot("mat", snap, blocks)   # content-addressed, stored once
    assert repo.load_blocks(snap["snapshot_id"]) == blocks
    assert repo.get_snapshot(snap["snapshot_id"])["block_count"] == len(blocks)


# -- one-topic check ----------------------------------------------------------------------

def overview_then_item(item_sents=12, tail=False):
    """An overview paragraph, then the details of its first item (and a one-line wrap-up), then a short next
    section (md lines)."""
    overview = "自然語言處理的發展可以分成四個技術階段，每個階段都改變了模型設計的思維。" * 6
    item = "第一階段的規則式方法依賴專家手寫的語法規則與詞彙辭典，可解釋但難以擴展。" * item_sents
    lines = ["# 技術演進脈絡", overview, "", item, ""] + (["以上就是第一階段。", ""] if tail else []) + ["# 下一節", "下一節的內容很短。"]
    return extract_blocks("m", "line", 1, len(lines), lines)[1]


def one_unit_then_rest(blocks):
    nxt = next(i for i, b in enumerate(blocks) if b.text == "下一節")
    return {"units": [{"blocks": [f"B1-B{nxt}"], "reason": "技術演進"},
                      {"blocks": [f"B{nxt + 1}-B{len(blocks)}"], "reason": "下一節"}]}, nxt


def summary(*topics):
    return {"paragraphs": [{"topic": t, "aspect": f"第 {k} 段"} for k, t in enumerate(topics, 1)]}


async def test_a_unit_with_two_topics_is_split_from_paragraph_summaries():
    blocks = overview_then_item()
    first, nxt = one_unit_then_rest(blocks)
    item = next(i for i, b in enumerate(blocks) if b.text.startswith("第一階段"))
    b = Scripted([first, summary("技術演進總覽", "規則式方法"), {"new_topic_at": [2]}])
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    # the short last unit is not asked; the check asks for summaries first, then where topics change
    assert [r["meta"]["step"] for r in b.requests] == ["segment_blocks", "segment_topic_summary", "segment_topic"]
    listing = b.requests[1]["messages"][0]["content"]
    assert "【P1】\n技術演進脈絡" in listing and "【P2】\n第一階段" in listing and "【P3】" not in listing   # heading + its paragraph
    decide = b.requests[2]["messages"][0]["content"]
    assert "P1：大主題「技術演進總覽」｜第 1 段" in decide and "第一階段的規則式方法" not in decide            # summaries only
    assert groups_of(plan, blocks) == [list(range(item)), list(range(item, nxt)), list(range(nxt, len(blocks)))]
    assert [u.boundary_reason for u in plan.units] == ["技術演進總覽", "規則式方法", "下一節"]
    assert any("單題檢查" in w and "2 個主題" in w for w in plan.warnings)


async def test_one_topic_a_too_fine_split_or_an_invalid_answer_keeps_the_unit():
    blocks = overview_then_item()
    first, nxt = one_unit_then_rest(blocks)
    for starts in ({"new_topic_at": []}, {"new_topic_at": [1]}):     # the example copied / P1 already opens the unit
        b = Scripted([first, summary("技術演進", "技術演進"), starts])
        deps, _ = deps_for(b)
        plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
        assert len(b.requests) == 3 and groups_of(plan, blocks)[0] == list(range(nxt))
        assert plan.units[0].boundary_reason == "技術演進"

    tailed = overview_then_item(tail=True)
    first_t, nxt_t = one_unit_then_rest(tailed)
    b = Scripted([first_t, summary("技術演進", "技術演進", "小結"), {"new_topic_at": [3]}])   # a one-line part
    deps, _ = deps_for(b)
    plan = await seg.plan_units(tailed, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    assert groups_of(plan, tailed)[0] == list(range(nxt_t)) and any("太短" in w for w in plan.warnings)

    wrong_count = summary("技術演進")                                   # one summary for two paragraphs, twice
    b = Scripted([first, wrong_count, wrong_count])
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    assert groups_of(plan, blocks)[0] == list(range(nxt)) and any("單題檢查無效" in w for w in plan.warnings)


async def test_one_topic_check_skips_short_units_and_old_prompt_snapshots():
    blocks = overview_then_item(item_sents=1)                       # under TOPIC_CHECK_MIN_CHARS
    first, nxt = one_unit_then_rest(blocks)
    assert not seg.needs_topic_check(blocks, list(range(nxt)))
    b = Scripted([first])
    deps, _ = deps_for(b)
    await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    assert len(b.requests) == 1

    blocks = overview_then_item()
    first, nxt = one_unit_then_rest(blocks)
    for missing in ("segment_topic", "segment_topic_summary"):
        b = Scripted([first])
        deps, _ = deps_for(b)
        deps.prompts = {k: v for k, v in deps.prompts.items() if k != missing}
        plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
        assert len(b.requests) == 1 and groups_of(plan, blocks)[0] == list(range(nxt))


def test_a_cut_right_after_a_heading_moves_back_before_it():
    """qwen3-vl:4b cut between "A. 第一階段" and its first sentence; moving the cut forward (as for other glued pairs)
    pulled the heading and that sentence into the overview unit. A heading goes with what follows it."""
    lines = ["# 技術演進脈絡", "總覽的第一句。", "", "## A. 第一階段", "第一階段的第一句。", "", "第一階段的第二段。"]
    blocks = extract_blocks("m", "line", 1, len(lines), lines)[1]
    head = next(i for i, b in enumerate(blocks) if b.text == "A. 第一階段")
    groups, notes = seg.normalize_groups(blocks, [list(range(head + 1)), list(range(head + 1, len(blocks)))], 0, len(blocks))
    assert groups == [list(range(head)), list(range(head, len(blocks)))] and any("小標題" in n for n in notes)
    groups, _ = seg.normalize_groups(blocks, [[0], list(range(1, len(blocks)))], 0, len(blocks))   # a lone heading
    assert groups == [list(range(len(blocks)))]


async def test_a_new_topic_after_a_heading_starts_at_the_heading():
    overview = "自然語言處理的發展可以分成四個技術階段，每個階段都改變了模型設計的思維。" * 6
    item = "第一階段的規則式方法依賴專家手寫的語法規則與詞彙辭典，可解釋但難以擴展。" * 12
    lines = ["# 技術演進脈絡", overview, "", "## A. 第一階段", item, "", "# 下一節", "下一節的內容很短。"]
    blocks = extract_blocks("m", "line", 1, len(lines), lines)[1]
    first, nxt = one_unit_then_rest(blocks)
    head = next(i for i, b in enumerate(blocks) if b.text == "A. 第一階段")
    b = Scripted([first, summary("技術演進總覽", "規則式方法"), {"new_topic_at": [2]}])
    deps, _ = deps_for(b)
    plan = await seg.plan_units(blocks, deps, window_chars=10 ** 6, window_tokens=10 ** 6)
    assert groups_of(plan, blocks)[:2] == [list(range(head)), list(range(head, nxt))]


# -- units-v9: 區塊 outlines decide the units ------------------------------------------------------

def outline(*topics):
    return {"paragraphs": [{"topic": t, "aspect": f"第 {k} 段", "summary": f"摘要 {k}"} for k, t in enumerate(topics, 1)]}


def paragraphs(n):
    """n plain paragraphs (no headings), each long enough to be its own 區塊."""
    lines = []
    for k in range(1, n + 1):
        lines += [f"第{k}段：" + "這一段說明某個概念的一個面向，句子寫得夠長，可以自己成為一個區塊。" * 2, ""]
    return extract_blocks("m", "line", 1, len(lines), lines)[1]


def test_chunks_are_paragraphs_a_heading_stays_with_its_first_paragraph():
    blocks = overview_then_item()
    chunks = seg.make_chunks(blocks)
    texts = [[blocks[i].text[:6] for i in c] for c in chunks]
    assert texts == [["技術演進脈絡", "自然語言處理"], ["第一階段的規"], ["下一節", "下一節的內容"]]
    lead = extract_blocks("m", "line", 1, 4, ["以下是概覽：", "", "自然語言處理的發展可以分成四個技術階段，每個階段都改變了模型設計的思維。" * 2])[1]
    assert seg.make_chunks(lead) == [[0, 1]]                       # a one-line lead-in joins the paragraph after it


async def test_units_are_decided_from_chunk_outlines_and_carry_their_chunks():
    blocks = overview_then_item()
    b = Scripted([outline("技術演進總覽", "規則式方法", "下一節"), {"new_topic_at": [2, 3]}])
    deps, _ = deps_for(b, outline=True)
    plan = await seg.plan_units(blocks, deps)
    assert [r["meta"]["step"] for r in b.requests] == ["segment_outline", "segment_topic"]
    listing = b.requests[0]["messages"][0]["content"]
    assert "【P1】\n技術演進脈絡" in listing and "【P3】\n下一節" in listing
    decide = b.requests[1]["messages"][0]["content"]
    assert "P2：大主題「規則式方法」｜第 2 段" in decide and "專家手寫" not in decide       # outlines only, never source
    assert [u.boundary_reason for u in plan.units] == ["技術演進總覽", "規則式方法", "下一節"]
    assert [[c.summary for c in u.chunks] for u in plan.units] == [["摘要 1"], ["摘要 2"], ["摘要 3"]]
    by_id = {x.block_id: x for x in blocks}
    quotes = seg.to_quotes("line", plan, by_id)
    assert [len(q["chunks"]) for q in quotes] == [1, 1, 1] and quotes[1]["chunks"][0]["text"].startswith("第一階段")
    assert quotes[1]["chunks"][0]["topic"] == "規則式方法"


async def test_an_invalid_outline_or_decision_falls_back_without_losing_text():
    blocks = overview_then_item()
    b = Scripted([outline("只有一個"), outline("只有一個"), {"new_topic_at": []}])     # wrong count twice
    deps, _ = deps_for(b, outline=True)
    plan = await seg.plan_units(blocks, deps)
    assert any("區塊摘要無效" in w for w in plan.warnings)
    assert sum(len(u.block_ids) for u in plan.units) == len(blocks)
    assert plan.units[0].chunks[0].summary.startswith("技術演進脈絡")        # the opening words stand in

    b = Scripted([outline("總覽", "規則式方法", "規則式方法"), "不是 JSON", "還是不是"])  # decision fails twice
    deps, _ = deps_for(b, outline=True)
    plan = await seg.plan_units(blocks, deps)
    assert any("主題判斷無效" in w for w in plan.warnings)
    assert [len(u.chunks) for u in plan.units] == [1, 2]                     # cut where the topic wording changes


async def test_the_last_topic_of_a_batch_is_judged_again_with_the_next(monkeypatch):
    monkeypatch.setattr(seg, "TOPIC_BATCH", 3)
    blocks = paragraphs(6)
    b = Scripted([outline("A", "A", "B", "B", "B", "C"), {"new_topic_at": [3]}, {"new_topic_at": []}, {"new_topic_at": [2]}])
    deps, _ = deps_for(b, outline=True)
    plan = await seg.plan_units(blocks, deps)
    decides = [r["messages"][0]["content"] for r in b.requests[1:]]
    assert "P1：大主題「B」" in decides[1]            # the second batch opens at the topic that may continue
    assert "P1：大主題「B」" in decides[2] and "P2：大主題「C」" in decides[2]   # no change seen: overlap one 區塊
    assert [len(u.chunks) for u in plan.units] == [2, 3, 1]
