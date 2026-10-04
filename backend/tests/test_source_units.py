"""Source blocks and unit planning (P2): text is kept by the program, plans are validated, rules are a valid
fallback, windows / seams never split protected relations, and old databases migrate in place."""
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


def deps_for(backend):
    rt = ModelRuntime(ModelSnapshot("mock", "", "", "", "", 0.2, 512, 8192), BackendPool(build=lambda c: backend))
    sink = ListSink()
    b = load_bundle()
    return seg.PlannerDeps(make_ctx=lambda task, key: TaskContext(rt, CallBudget.for_task(task), sink),
                           prompts=b["prompts"], prompt_version=b["hash"], material_title="t"), sink


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
        page = units[b.locator.start - 1]
        assert page[b.locator.char_start:b.locator.char_end] == b.text
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


def test_page_break_mid_sentence_is_glued(manifest):
    _, _, _, blocks = blocks_of(manifest, "cross-page-condition")
    i = next(i for i, b in enumerate(blocks) if b.continues)
    assert blocks[i].text.endswith("連續；") and blocks[i + 1].text.startswith("第二，")
    assert seg.glues(blocks)[i] == "句子跨頁／跨區塊延續"


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


def test_windows_never_cut_glued_pairs_and_carry_read_only_context(manifest):
    _, _, _, blocks = blocks_of(manifest, "list-tcp")
    g = seg.glues(blocks)
    for size in (30, 60, 100, 200):
        ws = seg.plan_windows(blocks, size)
        assert ws[0].lo == 0 and ws[-1].hi == len(blocks)
        for a, b in zip(ws, ws[1:]):
            assert a.hi == b.lo and not g[a.hi - 1]
            assert b.before == list(range(max(0, b.lo - seg.CONTEXT_BLOCKS), b.lo))


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


async def test_invalid_plan_is_repaired_once_then_falls_back_to_rules(manifest):
    _, _, _, blocks = blocks_of(manifest, "formula-where")
    f = next(i for i, b in enumerate(blocks) if b.kind == "formula")
    split_formula = {"units": [{"blocks": [f"B{i + 1}" for i in range(0, f)]},
                               {"blocks": [f"B{i + 1}" for i in range(f, len(blocks))]}]}
    b = Scripted([split_formula, split_formula])
    deps, sink = deps_for(b)
    plan = await seg.plan_units(blocks, deps)
    assert len(b.requests) == 2 and plan.planner == "rules" and plan.warnings
    assert "不可切開" in b.requests[1]["messages"][-2]["content"] or "不可切開" in json.dumps(b.requests[1], ensure_ascii=False)
    assert seg.validate_groups(blocks, [[next(i for i, x in enumerate(blocks) if x.block_id == bid) for bid in u.block_ids]
                                        for u in plan.units]) == []


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


async def test_connective_seam_asks_the_model_once_and_can_merge(manifest):
    _, _, _, blocks = blocks_of(manifest, "cross-window-argument")
    n = len(blocks)
    windows = seg.plan_windows(blocks, 150)
    outputs = []
    for w in windows:
        outputs.append({"units": [{"blocks": [f"B{i - w.lo + 1}"]} for i in range(w.lo, w.hi)]})
    b = Scripted(outputs + [{"merge": True, "reason": "同一論證"}] * 5)
    deps, sink = deps_for(b)
    plan = await seg.plan_units(blocks, deps, window_chars=150)
    seam_calls = [r for r in sink.records if r.task == "seam"]
    assert seam_calls and all(r.validated["merge"] for r in seam_calls)
    assert len(plan.units) < n


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
