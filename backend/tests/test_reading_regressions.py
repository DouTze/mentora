"""Release-gate regressions (R0): the cases reproduced in the 2026-10-04 review, kept as data in
evals/reading/regressions/cases.json. Model outputs are scripted by role; the re-check is adversarial (it always
claims the draft is fine, quoting the guide), so a case passes only if the program's own evidence rules block it.
Every case also states the expected outcome for a correct draft, so the gate cannot pass by refusing everything."""
import json
import re
from pathlib import Path

import pytest

from app.llm.base import LLMBackend, LLMResult
from app.llm.runtime import BackendPool, ListSink, ModelRuntime, ModelSnapshot
from app.reading import agent as ra
from app.reading.prompts import load_bundle
from app.reading.tools import ReadingScope
from app.workflows import invoke
from app.workflows.reading_graph import ReadingDeps, build_reading_graph, initial_state

FIXTURES = json.loads((Path(__file__).resolve().parent.parent / "evals/reading/regressions/cases.json").read_text(encoding="utf-8"))
BUNDLE = load_bundle()
SNAP = ModelSnapshot("mock", "", "", "", "", 0.2, 512, 8192)


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[。！？])", text, maxsplit=1)[0]


class RoleScripted(LLMBackend):
    """Plays every role from one case. The source stage is correct (one claim per sentence, the case's 中文課文);
    the drafter always writes the case's draft (revisions included); the auditor uses the case's audit if it has
    one, otherwise it is adversarial — it calls every claim covered, quoting the guide's first sentence; the
    re-check always says "no problem" with that quote. A bad draft must be stopped by the program's evidence rules."""
    name = model = "scripted"

    def __init__(self, case: dict):
        self.case, self.steps = case, []

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
        step = (meta or {}).get("step")
        self.steps.append(step)
        c = self.case
        quote = first_sentence(c["guide"])
        cn = c["chinese_text"]
        if step in ("understand", "revise_source"):
            out = {"translations": [] if cn is None else [{"id": sid, "zh": cn} for sid, _ in meta["segments"]],
                   "claims": [{"id": f"C{i}", "sentences": [sid], "statement": "", "function": "claim"}
                              for i, (sid, _) in enumerate(meta["segments"], 1)]}
        elif step == "source_audit":
            out = {"sentences": [{"id": sid, "verdict": "faithful"} for sid, _ in meta["sentences"]],
                   "claims": [{"id": x, "verdict": "faithful"} for x in meta["claim_ids"]]}
        elif step == "plan_bridges":
            out = {"learning_goal": "讀懂", "bridges": []}
        elif step in ("draft", "revise_body"):
            out = {"guide": c["guide"], "key_points": [], "check": "", "bridges_used": []}
        elif step == "body_audit":
            out = c["body_audit"] if "body_audit" in c else {
                "claims": [{"id": x["id"], "verdict": "covered", "guide_quote": quote} for x in meta["claims"]],
                "bridges": [], "additions": [], "uncovered_source": [], "coherence": "pass"}
        elif step == "verify":
            out = {"checks": [{"item": k, "verdict": "no_problem", "guide_quote": quote, "reason": "正文已經寫到"}
                              for k in range(1, 6)]}
        else:
            raise AssertionError(step)
        return LLMResult(text=json.dumps(out, ensure_ascii=False), latency_ms=1)


async def run_case(c: dict) -> tuple[dict, RoleScripted]:
    b = RoleScripted(c)
    pool = BackendPool(build=lambda _: b)
    q = {"id": "q", "index": 0, "unit_id": "u", "label": "第 1 行", "heading": None, "text": c["source"]}
    deps = ReadingDeps(runtime_for=lambda s: ModelRuntime(SNAP, pool), prompts_for=lambda s: BUNDLE["prompts"],
                       scope_for=lambda s, i: ReadingScope(quotes=[q], current_index=i), sink=ListSink())
    t = ra.ReadingTaskInput(session_id="s", unit_id="u", index=0, total=1, label="第 1 行", text=c["source"],
                            material_title="m", range_label="r", model_config_hash="h", prompt_bundle_hash="p")
    out = await invoke(build_reading_graph(deps), initial_state(t, op=f"op:{c['id']}"), {"configurable": {"thread_id": c["id"]}})
    return out, b


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=[c["id"] for c in FIXTURES["cases"]])
async def test_release_gate(case):
    out, b = await run_case(case)
    assert (out["status"] == "accepted") is case["expect"]["published"], (b.steps, out.get("findings"), out.get("error"))
    if not case["expect"]["published"]:
        assert out["artifact"] is None and out["error"]["detail"]
        if out["error"]["code"] != "review_invalid":
            assert out["draft"]["guide"] == case["guide"]   # the draft is kept, not shown
    assert out["budget"]["calls"] <= out["budget"]["max_calls"]


def test_empty_review_is_not_a_review():
    with pytest.raises(Exception):
        ra.ReviewResult.model_validate({})
    with pytest.raises(Exception):
        ra.ReviewResult.model_validate({"issues": []})


def test_chinese_text_never_answers_for_the_guide():
    c = next(x for x in FIXTURES["cases"] if x["id"] == "cross-field-condition")
    t = ra.ReadingTaskInput(session_id="s", unit_id="u", index=0, total=1, label="l", text=c["source"], material_title="m",
                            range_label="r", model_config_hash="h", prompt_bundle_hash="p")
    fs = ra.validate_draft(t, ra.GuideDraft(chinese_text=c["chinese_text"], guide=c["guide"]))
    guide_formula = [f for f in fs if f.target == "guide" and f.code == "wrong_formula"]
    assert len(guide_formula) == 2 and all(f.status == "confirmed" for f in guide_formula)
    assert not [f for f in fs if f.target == "chinese_text"]   # the 中文課文 itself is fine


def test_text_overlap_does_not_withdraw_a_reversal():
    c = next(x for x in FIXTURES["cases"] if x["id"] == "negation-reversal")
    review = ra.ReviewResult.model_validate({"checks": [{"check": k, "result": "pass"} for k in ra.expected_checks(False)],
                                             "issues": [{"target": "guide", **c["review"]["issues"][0]}]})
    fs = ra.review_findings(review, {"guide": c["guide"]}, c["source"])
    assert fs[0].severity == "major" and fs[0].status == "confirmed"
    v = ra.VerifyResult(checks=[ra.VerifyCheck(item=1, verdict="no_problem", guide_quote=first_sentence(c["guide"]))])
    after = ra.apply_verify(fs, ra.to_verify(fs), v, {"guide": c["guide"]}, c["source"])
    assert after[0].severity == "major" and "否定" in after[0].detail


def test_markers_ignore_code_and_treat_only_if_not_only_as_a_condition():
    from app.reading.markers import parity, profile
    code = "```python\nresult = [n**2 for n in range(10) if n % 2 == 0]\n```"
    assert "condition" not in profile(code)                          # `if` inside code is not a condition
    assert parity("This paragraph explains the idea in words only.", "這段只用文字說明。") == []
    assert parity("Only if x > 0 is the formula valid.", "這個公式有效。")    # "only if" still is a condition


def test_a_negation_of_the_next_claim_in_the_same_guide_sentence_is_not_this_claims():
    """A guide sentence that carries two source sentences: the 並不 / 無法 of the second one are not added to the first."""
    from app.reading.gates import coverage_parity
    source = ("• ELIZA：早期的對話式AI 系統，通過關鍵詞比對進行簡單的對話模擬。"
              "ELIZA能夠進行特定情境下的對話，但並不具備語意理解，無法處理複雜的語境。")
    claim = "• ELIZA：早期的對話式AI 系統，通過關鍵詞比對進行簡單的對話模擬。"
    guide = ("代表技術包括ELIZA：早期的對話式AI系統，通過關鍵詞比對進行簡單的對話模擬，"
             "ELIZA能夠進行特定情境下的對話，但並不具備語意理解，無法處理複雜的語境。")
    assert coverage_parity(claim, guide, source) == []
    reversed_ = "ELIZA並不是早期的對話式AI系統，通過關鍵詞比對進行簡單的對話模擬。"
    assert any("多了否定" in m for m in coverage_parity(claim, reversed_, source))
    denied = "ELIZA是早期的對話式AI系統，通過關鍵詞比對進行簡單的對話模擬，這並不正確。"
    assert any("多了否定" in m for m in coverage_parity(claim, denied, source))


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
