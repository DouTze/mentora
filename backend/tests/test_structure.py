"""R4: book structure, complete reading units, parts and model batches are separate. A part holds at most one formal
third-level section; arguments, examples, enumerations and code explanations stay whole (or, when too long, stay
adjacent in one part with a recorded relation); windows are computing boundaries only; the text rebuilds exactly."""
import json
import sqlite3

import pytest

from app.db import Database
from app.persistence.repository import Repository
from app.reading import segmentation as seg
from app.reading.schemas import SegmentationPlan, SourceUnit
from app.reading.source import extract_blocks
from evals import reading_eval as ev

FIX = json.loads((ev.EVAL_DIR / "regressions" / "cases.json").read_text())


def rules_quotes(lines: list[str], unit: str = "line"):
    _, blocks = extract_blocks("m", unit, 1, len(lines), lines)
    groups = seg.rules_groups(blocks)
    plan = SegmentationPlan(units=[SourceUnit(unit_id=f"u{i}", ordinal=i, block_ids=[blocks[j].block_id for j in g],
                                              segmentation_version=seg.SEGMENTATION_VERSION) for i, g in enumerate(groups)],
                            relations=seg.glue_plan(blocks)[1])
    quotes = seg.to_quotes(unit, plan, {b.block_id: b for b in blocks})
    return blocks, plan, quotes


@pytest.mark.parametrize("case", FIX["structure_cases"], ids=[c["id"] for c in FIX["structure_cases"]])
def test_regression_structure(case):
    _, _, quotes = rules_quotes(case["lines"])
    parts, contracts = seg.plan_parts(quotes)
    assert len(parts) == case["expect"]["parts"]
    assert [c.section_path for c in contracts] == [["第 1 章", "1.1 機率", "1.1.1 樣本空間"], ["第 1 章", "1.1 機率", "1.1.2 事件"]]
    assert contracts[1].boundary_reason.startswith("新的小節")


def test_deeper_headings_stay_inside_their_section_and_body_inherits_the_path():
    lines = ["## 2.3 梯度", "", "梯度指向函數上升最快的方向。", "", "### 2.3.1 方向導數", "", "方向導數是沿某方向的變化率。",
             "", "#### 例子", "", "例如在平面上沿 x 軸方向的方向導數就是偏導數。", "", "這個結果之後會用到。"]
    blocks, _, quotes = rules_quotes(lines)
    assert [b.heading_level for b in blocks if b.kind == "heading"] == [2, 3, 4]
    parts, contracts = seg.plan_parts(quotes)
    assert [c.section_path for c in contracts] == [["2.3 梯度"], ["2.3 梯度", "2.3.1 方向導數"]]   # #### is not a part
    assert quotes[-1]["section"] == ["2.3 梯度", "2.3.1 方向導數"]


def test_layout_guessed_pdf_headings_do_not_define_sections():
    pages = ["第三章 均值定理\n\n均值定理把平均變化率和瞬間變化率連結起來。\n\n例外情形\n\n若函數不連續，結論不一定成立。"]
    blocks, _, quotes = rules_quotes(pages, unit="page")
    heads = [b for b in blocks if b.kind == "heading"]
    assert heads and all(b.structure_confidence == "low" and b.heading_level is None for b in heads)
    parts, contracts = seg.plan_parts(quotes)
    assert len(parts) == 1 and contracts[0].structure_confidence == "low" and contracts[0].section_path == []


def test_cross_window_argument_stays_one_unit_and_matches_gold():
    manifest = ev.load_manifest()
    case = ev.select_cases(manifest, ["cross-window-argument"])[0]
    unit, units = ev.load_units(case)
    _, blocks = extract_blocks(case["id"], unit, 1, len(units), units)
    for w in seg.plan_windows(blocks, case["window_chars"]):   # a model batch never cuts the argument either
        g = seg.glues(blocks)
        assert w.hi == len(blocks) or not g[w.hi - 1]
    _, _, quotes = rules_quotes(units)
    rep = ev.check_segmentation(case, unit, units, [{"text": q["text"], "heading": q.get("heading")} for q in quotes])
    assert rep.integrity_ok and not rep.together_violations and not rep.protected_violations, rep.as_dict()
    u3 = next(q for q in quotes if q["text"].startswith("然而"))
    assert "第二個前提是" in u3["text"] and "因此，只有在獨立" in u3["text"]


def test_examples_code_explanations_and_where_clauses_stay_with_what_they_belong_to():
    lines = ["機會成本是選擇某個方案時放棄的最佳替代方案的價值。", "", "例如，花一個晚上讀書的機會成本，是那個晚上能打工賺到的錢。",
             "", "```python", "x = [1, 2, 3]", "```", "", "這段程式建立一個串列。", "", "$$y = mx + b$$", "",
             "其中 m 是斜率，b 是截距。"]
    blocks, _, quotes = rules_quotes(lines)
    texts = [q["text"] for q in quotes]
    assert any("機會成本是" in t and "例如" in t for t in texts)
    assert any("x = [1, 2, 3]" in t and "這段程式建立" in t for t in texts)
    assert any("y = mx + b" in t and "其中 m 是斜率" in t for t in texts)


def test_an_oversize_argument_is_split_into_adjacent_units_kept_in_one_part():
    long_premise = "前提的說明。" * 160                      # ~960 chars
    lines = ["第一個前提是樣本彼此獨立。" + long_premise, "", "第二個前提是樣本數夠大。" + long_premise, "",
             "因此，只有兩個前提同時成立時，結論才成立。"]
    blocks, plan, quotes = rules_quotes(lines)
    assert len(quotes) >= 2 and plan.relations and "超過" in plan.relations[0]["note"]
    assert any(q["depends_on_prev"] for q in quotes[1:])
    parts, _ = seg.plan_parts(quotes, part_chars=1000)
    dependent = [q for q in quotes if q["depends_on_prev"]]
    assert all(q["part"] == quotes[q["index"] - 1]["part"] for q in dependent)   # never split across parts
    assert "".join(q["text"] for q in quotes).replace("\n", "") == "".join(lines)


def test_a_model_plan_that_splits_an_argument_is_rejected():
    manifest = ev.load_manifest()
    case = ev.select_cases(manifest, ["cross-window-argument"])[0]
    unit, units = ev.load_units(case)
    _, blocks = extract_blocks(case["id"], unit, 1, len(units), units)
    split = [[i] for i in range(len(blocks))]
    errs = seg.validate_groups(blocks, split)
    assert any("論證的結論承接前提" in e for e in errs) and any("列舉" in e for e in errs)


def test_blocks_v1_rows_still_load_after_migration_v5(tmp_path):
    """A database written before R4 (no heading_level / structure_confidence / meta columns) migrates in place;
    its blocks load with unknown structure."""
    path = tmp_path / "old.sqlite3"
    db = Database(path)
    with sqlite3.connect(path) as c:
        c.execute("DELETE FROM schema_migrations WHERE version >= 5")
        cols = [r[1] for r in c.execute("PRAGMA table_info(source_blocks)")]
        keep = [x for x in cols if x not in ("heading_level", "structure_confidence", "meta")]
        c.execute(f"CREATE TABLE sb_old AS SELECT {','.join(keep)} FROM source_blocks")
        c.execute("DROP TABLE source_blocks")
        c.execute("ALTER TABLE sb_old RENAME TO source_blocks")
        c.execute("INSERT INTO source_snapshots VALUES ('snap-old','m','line',1,2,'blocks-v1','h',1,'2026-01-01')")
        c.execute("INSERT INTO source_blocks (block_id, snapshot_id, ordinal, kind, text, locator, text_hash, "
                  "extraction_version, parent_block_id, piece, continues) VALUES ('b1','snap-old',0,'heading','標題',?,"
                  "'h','blocks-v1',NULL,NULL,0)", (json.dumps({"unit": "line", "start": 1, "end": 1}),))
    repo = Repository(Database(path))
    [b] = repo.load_blocks("snap-old")
    assert b.heading_level is None and b.structure_confidence is None and b.meta == {} and b.extraction_version == "blocks-v1"
