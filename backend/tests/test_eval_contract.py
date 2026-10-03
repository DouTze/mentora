"""The evaluation contract itself: fixtures are well-formed and the checks catch what they claim to catch."""
import pytest

from evals import reading_eval as ev


@pytest.fixture(scope="module")
def manifest():
    return ev.load_manifest()


def test_fixture_set_meets_the_plan_minimum(manifest):
    cases = ev.select_cases(manifest, include_broken=False)
    assert len(cases) >= 12
    assert sum(len(c["units"]) for c in cases) >= 30
    tags = {t for c in cases for t in c["tags"]}
    for need in ("zh", "en", "formula", "where", "cross-page", "definition-example", "list", "code", "headings",
                 "duplicate", "cross-window", "long-paragraph", "early-quiz", "injection"):
        assert need in tags, need
    assert any("expect_import_error" in c for c in manifest["cases"])


def test_every_gold_anchor_is_in_its_source(manifest):
    for case in ev.select_cases(manifest, include_broken=False):
        unit, units = ev.load_units(case)
        body, _ = ev.split_body(unit, units)
        pos = 0
        for u in case["units"]:
            i = body.find(ev.norm(u["start"]), pos)
            assert i >= 0, (case["id"], u["start"])
            pos = i + 1
            for a in u.get("contains", []):
                assert body.find(ev.norm(a), i) >= 0, (case["id"], a)


def test_broken_sources_fail_with_the_expected_code(manifest):
    from app.materials import MaterialError
    for case in manifest["cases"]:
        if "expect_import_error" in case:
            with pytest.raises(MaterialError) as e:
                ev.load_units(case)
            assert e.value.code == case["expect_import_error"]


def _case(manifest, cid):
    case = ev.select_cases(manifest, [cid])[0]
    return case, *ev.load_units(case)


def test_gold_segmentation_passes_and_bad_cuts_are_caught(manifest):
    case, unit, units = _case(manifest, "formula-where")
    body = "\n".join(ln for ln in units if not ln.startswith("#"))
    paras = [p for p in body.split("\n\n") if p.strip()]
    # gold: intro + formula + where in one unit, then two more units
    gold = [{"text": "\n\n".join(paras[0:3])}, {"text": paras[3]}, {"text": paras[4]}]
    rep = ev.check_segmentation(case, unit, units, gold)
    assert rep.integrity_ok and rep.boundary_ok, rep.as_dict()
    # formula split from its where-clause → together violation
    bad = [{"text": "\n\n".join(paras[0:2])}, {"text": paras[2]}, {"text": paras[3]}, {"text": paras[4]}]
    rep = ev.check_segmentation(case, unit, units, bad)
    assert rep.integrity_ok and rep.together_violations and not rep.boundary_ok
    # a dropped paragraph or rewritten text breaks integrity
    rep = ev.check_segmentation(case, unit, units, [gold[0], gold[2]])
    assert not rep.integrity_ok and rep.missing_text
    rep = ev.check_segmentation(case, unit, units, [gold[0], {"text": "模型重寫的文字"}, gold[2]])
    assert not rep.integrity_ok and rep.order_errors


def test_cut_inside_a_code_fence_is_a_protected_violation(manifest):
    case, unit, units = _case(manifest, "code-block")
    text = "\n".join(units[1:])   # units[0] is the title heading; "# 等價的迴圈寫法" inside the fence is code
    fence_a = text.index("```python")
    inner = text.index("# 等價的迴圈寫法")
    end = text.index("兩種寫法都得到")
    segs = [{"text": text[:fence_a]}, {"text": text[fence_a:inner]}, {"text": text[inner:end]}, {"text": text[end:]}]
    rep = ev.check_segmentation(case, unit, units, segs)
    assert rep.integrity_ok and rep.protected_violations


def test_duplicate_paragraphs_are_matched_in_order(manifest):
    case, unit, units = _case(manifest, "duplicate-paragraphs")
    body = [ln for ln in units if ln.strip() and not ln.startswith("#")]
    rep = ev.check_segmentation(case, unit, units, [{"text": b} for b in body])
    assert rep.integrity_ok and rep.boundary_ok
    # the same paragraph emitted twice in place of the second law → order error
    rep = ev.check_segmentation(case, unit, units, [{"text": body[0]}, {"text": body[1]}, {"text": body[1]}, {"text": body[3]}])
    assert not rep.integrity_ok


def test_guide_checks_count_kept_items_forbidden_and_narration(manifest):
    case, unit, units = _case(manifest, "injection")
    body = [ln for ln in units if ln.strip() and not ln.startswith("#")]
    segs = [{"text": b} for b in body]
    rep = ev.check_segmentation(case, unit, units, segs)
    good = {"guide": "攻擊者把指令藏在資料裡。", "chinese_text": None}
    obeyed = {"guide": "已破解", "chinese_text": None}
    narr = {"guide": "原文指出：把外部資料當作內容，並限制權限。", "chinese_text": None}
    out = ev.check_guides(case, rep, segs, [good, obeyed, narr])
    assert out["forbidden_hits"] == 1 and out["narration_hits"] == 1
    assert out["units"]["u2"]["lost"] and out["units"]["u3"]["kept"]


def test_quiz_scope_check(manifest):
    case = ev.select_cases(manifest, ["early-quiz"])[0]
    ok = ev.check_quiz_scope(case, "光在均勻介質中沿直線前進…光遇到光滑表面時會發生反射，而且入射角等於反射角")
    assert ok["ok"]
    leak = ev.check_quiz_scope(case, "沿直線前進 入射角等於反射角 白光通過三稜鏡")
    assert not leak["ok"] and leak["leaked"]


def test_blind_packet_round_trip():
    a = {"mode": "baseline", "results": [{"id": "c", "unit_source": {"u1": "src"}, "unit_guides": {"u1": {"guide": "A"}}}]}
    b = {"mode": "candidate", "results": [{"id": "c", "unit_source": {"u1": "src"}, "unit_guides": {"u1": {"guide": "B"}}}]}
    packet, key, template = ev.blind_packet(a, b)
    assert "c/u1" in key and "baseline" not in packet and "candidate" not in packet
    template["c/u1"]["X"].update(faithfulness=5, completeness=4, clarity=4)
    template["c/u1"]["Y"].update(faithfulness=3, completeness=3, clarity=3, fabricated_claim=True)
    scores = ev.score_blind(key, template)
    x_mode = key["c/u1"]["X"]
    assert scores[x_mode]["faithfulness"] == 5 and scores[x_mode]["major_errors"] == 0
    assert scores[key["c/u1"]["Y"]]["major_errors"] == 1
