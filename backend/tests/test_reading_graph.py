"""Guided-reading subgraph (P3): program validation + model review decide publication, one revision, bounded
calls, read-only scoped tools, resume at the failed node, replies reused after a crash."""
import json

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.db import Database
from app.llm.base import LLMBackend, LLMError, LLMResult
from app.llm.mock import MockBackend
from app.llm.runtime import BackendPool, ListSink, ModelRuntime, ModelSnapshot
from app.persistence.repository import DbSink, Repository
from app.reading import agent as ra
from app.reading.prompts import load_bundle
from app.reading.tools import ReadingScope, ScopeError, build_tools
from app.workflows import invoke
from app.workflows.reading_graph import ReadingDeps, ReadingUnavailable, build_reading_graph, initial_state

BUNDLE = load_bundle()
SNAP = ModelSnapshot("mock", "", "", "", "", 0.2, 512, 8192)

QUOTES = [
    {"id": "quote-01", "index": 0, "unit_id": "u-a", "label": "第 1 行", "heading": None,
     "text": "若要使用均值定理，函數必須在閉區間上連續，並在開區間內可微。"},
    {"id": "quote-02", "index": 1, "unit_id": "u-b", "label": "第 3 行", "heading": None,
     "text": "在這兩個條件下，至少存在一點 $c$，使 $f'(c) = \\frac{f(b)-f(a)}{b-a}$。"},
    {"id": "quote-03", "index": 2, "unit_id": "u-c", "label": "第 5 行", "heading": None,
     "text": "若任一條件不成立，這樣的 c 就不一定存在。"},
]


class ByStep(LLMBackend):
    """Scripted outputs per meta['step']; records every call."""
    name, model = "scripted", "scripted"

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
        step = (meta or {}).get("step")
        self.calls.append({"step": step, "prompt": messages[-1]["content"] if messages[-1]["role"] == "user" else messages[-2]["content"]})
        out = self.script[step].pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResult(text=out if isinstance(out, str) else json.dumps(out, ensure_ascii=False), latency_ms=1)

    def steps(self):
        return [c["step"] for c in self.calls]


PLAN = {"definitions": [], "claims": ["存在 c"], "conditions": ["閉區間連續", "開區間可微"], "formulas": [],
        "reasoning": [], "prerequisites": [], "needs_context": [], "limits": []}
GOOD = {"chinese_text": None, "guide": "函數在閉區間上連續、在開區間內可微時，至少存在一點 $c$ 使 $f'(c) = \\frac{f(b)-f(a)}{b-a}$。",
        "key_points": ["均值定理的條件"], "check": "兩個條件是什麼？"}
MISSING = {**GOOD, "guide": "函數在閉區間上連續時，至少存在一點 $c$ 使 $f'(c) = \\frac{f(b)-f(a)}{b-a}$。"}
NO_FORMULA = {**GOOD, "guide": "在這兩個條件下，存在一點使導數等於平均變化率，這是均值定理的結論。"}
MAJOR = {"issues": [{"code": "missing_condition", "severity": "major", "source_excerpt": "開區間內可微",
                     "draft_excerpt": "…", "suggested_fix": "補上可微條件"}]}


def make(backend, *, repo=None, index=1, scope=None):
    pool = BackendPool(build=lambda c: backend)
    sink = DbSink(repo) if repo else ListSink()
    deps = ReadingDeps(runtime_for=lambda sid: ModelRuntime(SNAP, pool), prompts_for=lambda sid: BUNDLE["prompts"],
                       scope_for=lambda sid, i: scope or ReadingScope(quotes=QUOTES, current_index=i), sink=sink, repo=repo)
    q = QUOTES[index]
    task = ra.ReadingTaskInput(session_id="s1", unit_id=q["unit_id"], index=index, total=3, label=q["label"], text=q["text"],
                               block_ids=[f"b{index}"], material_title="微積分", range_label="第 1–5 行",
                               model_config_hash=SNAP.config_hash, prompt_bundle_hash=BUNDLE["hash"])
    return deps, task, sink


async def run(backend, *, index=1, repo=None, checkpointer=None, thread="t", scope=None):
    deps, task, sink = make(backend, repo=repo, index=index, scope=scope)
    g = build_reading_graph(deps, checkpointer)
    cfg = {"configurable": {"thread_id": thread}}
    out = await invoke(g, initial_state(task, op=f"s1:c1:guide:{task.unit_id}:1"), cfg)
    return out, sink


async def test_mock_guide_is_accepted_with_three_calls():
    b = MockBackend()
    out, sink = await run(b, index=0)
    assert out["status"] == "accepted" and out["budget"]["calls"] == 3
    assert [r.node for r in sink.records] == ["analyze", "draft", "review"]
    a = out["artifact"]
    assert a["review_status"] == "passed" and a["unit_id"] == "u-a" and a["source_refs"] == ["b0"]
    assert a["prompt_version"] == BUNDLE["hash"] and a["workflow_version"] == ra.READING_WORKFLOW_VERSION


async def test_review_major_triggers_one_revision_then_accepts():
    b = ByStep({"analyze": [PLAN], "context": [{"action": "ready", "calls": [], "reason": ""}],
                "draft": [MISSING], "review": [MAJOR, {"issues": []}], "revise": [GOOD]})
    out, _ = await run(b)
    assert out["status"] == "accepted" and out["artifact"]["revised"] is True
    assert b.steps() == ["analyze", "context", "draft", "review", "revise", "review"]
    revise_prompt = b.calls[4]["prompt"]
    assert "missing_condition" in revise_prompt and "補上可微條件" in revise_prompt and "開區間內可微" in revise_prompt
    assert len(out["history"]) == 1


async def test_major_issue_after_revision_is_not_published():
    b = ByStep({"analyze": [PLAN], "context": [{"action": "ready", "calls": [], "reason": ""}],
                "draft": [MISSING], "review": [MAJOR, MAJOR], "revise": [MISSING],
                "verify": [{"checks": [{"item": 1, "covered": True, "draft_quote": "函數在開區間內可微"}]}]})   # not in the draft
    out, _ = await run(b)
    assert b.steps()[-1] == "verify"
    assert out["status"] == "failed" and out["artifact"] is None and out["draft"]["guide"]
    assert out["error"]["code"] == "quality_check_failed" and "可微" in out["error"]["detail"]
    assert out["budget"]["calls"] <= 8


async def test_program_validation_catches_a_dropped_formula_before_review():
    b = ByStep({"analyze": [PLAN], "context": [{"action": "ready", "calls": [], "reason": ""}],
                "draft": [NO_FORMULA], "revise": [GOOD], "review": [{"issues": []}]})
    out, _ = await run(b)
    assert out["status"] == "accepted"
    assert b.steps() == ["analyze", "context", "draft", "revise", "review"]   # no review of the invalid draft
    assert "wrong_formula" in b.calls[3]["prompt"]


async def test_reviewer_cannot_downgrade_a_formula_error():
    downgraded = {"issues": [{"code": "wrong_formula", "severity": "minor", "suggested_fix": "公式錯了"}]}
    b = ByStep({"analyze": [PLAN], "context": [{"action": "ready", "calls": [], "reason": ""}],
                "draft": [GOOD], "review": [downgraded, downgraded], "revise": [GOOD]})
    out, _ = await run(b)
    assert out["status"] == "failed"


async def test_review_claims_contradicted_by_the_texts_do_not_block():
    """A small reviewer cites text the draft already has, or a draft passage that is not in the draft."""
    already_there = {"issues": [{"code": "missing_condition", "severity": "major", "source_excerpt": "開區間內可微",
                                 "draft_excerpt": "", "suggested_fix": "補上可微"}]}
    phantom = {"issues": [{"code": "unsupported_claim", "severity": "major", "source_excerpt": "",
                           "draft_excerpt": "這個定理由牛頓在 1687 年證明", "suggested_fix": "刪除"}]}
    supplement = {**GOOD, "guide": GOOD["guide"] + "\n\n補充說明：均值定理也叫拉格朗日中值定理。"}
    flagged = {"issues": [{"code": "unsupported_claim", "severity": "major", "draft_excerpt": "均值定理也叫拉格朗日中值定理",
                           "suggested_fix": "原文沒有"}]}
    for draft, review in ((GOOD, already_there), (GOOD, phantom), (supplement, flagged)):
        b = ByStep({"analyze": [PLAN], "draft": [draft], "review": [review]})
        out, _ = await run(b, index=0)
        assert out["status"] == "accepted", out.get("error")
        codes = [w["code"] for w in out["artifact"]["warnings"]]
        assert codes and set(codes) <= {"review_unverified", "supplement_flagged"}


def test_context_selection_only_when_the_unit_points_elsewhere():
    deps, task, _ = make(MockBackend(), index=0)
    vague = ra.TeachingPlan(needs_context=["需要了解何謂價值"])
    pointed = ra.TeachingPlan(needs_context=["「這兩個條件」指前一段的內容"])
    assert not ra.needs_context(task, vague) and ra.needs_context(task, pointed)
    _, task1, _ = make(MockBackend(), index=1)    # "在這兩個條件下…"
    assert ra.needs_context(task1, vague)


async def test_minor_issues_publish_with_warnings():
    b = ByStep({"analyze": [PLAN], "context": [{"action": "ready", "calls": [], "reason": ""}], "draft": [GOOD],
                "review": [{"issues": [{"code": "narration", "severity": "major", "suggested_fix": "去掉旁白"}]}]})
    out, _ = await run(b)
    assert out["status"] == "accepted" and out["artifact"]["review_status"] == "passed_with_warnings"
    assert out["artifact"]["warnings"][0]["code"] == "narration"


async def test_referring_unit_reads_its_neighbour_through_a_tool():
    req = {"action": "read", "reason": "這兩個條件指前一段",
           "calls": [{"tool": "read_neighbor_units", "args": {"unit_id": "u-b", "before": 1, "after": 0}}]}
    b = ByStep({"analyze": [PLAN], "context": [req, {"action": "ready", "calls": [], "reason": ""}],
                "draft": [GOOD], "review": [{"issues": []}]})
    out, _ = await run(b)
    assert out["supplements"][0]["ok"] and "閉區間上連續" in out["supplements"][0]["content"]
    assert "閉區間上連續" in b.calls[3]["prompt"]   # the draft prompt carries the supplement
    assert out["artifact"]["source_refs"] == ["b1", "u-b"]


async def test_plain_unit_skips_context_selection():
    b = MockBackend()
    out, sink = await run(b, index=0)
    assert "context" not in [r.node for r in sink.records] and out["supplements"] == []


async def test_invalid_output_twice_fails_within_budget():
    b = ByStep({"analyze": ["nope", "still nope"]})
    out, _ = await run(b)
    assert out["status"] == "failed" and out["error"]["code"] == "invalid_model_output" and len(b.calls) == 2


async def test_unavailable_model_stops_at_that_node_and_resume_continues_there(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    repo = Repository(db)
    b = ByStep({"analyze": [PLAN], "context": [{"action": "ready", "calls": [], "reason": ""}],
                "draft": [LLMError("Ollama 連線失敗"), GOOD], "review": [{"issues": []}]})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:
        with pytest.raises(ReadingUnavailable):
            await run(b, repo=repo, checkpointer=cp)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:   # a fresh saver = restart
        deps, task, _ = make(b, repo=repo)
        g = build_reading_graph(deps, cp)
        out = await invoke(g, None, {"configurable": {"thread_id": "t"}})
    assert out["status"] == "accepted"
    assert b.steps() == ["analyze", "context", "draft", "draft", "review"]   # analyze not repeated
    assert out["budget"]["calls"] == 5   # the failed call still counts
    calls = repo.list_llm_calls("s1")
    assert [c["node"] for c in calls] == ["analyze", "context", "draft", "draft", "review"]
    assert calls[2]["error_kind"] == "unavailable" and calls[3]["validated"]["guide"]


async def test_replies_committed_before_a_lost_checkpoint_are_reused(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite3"))
    b = MockBackend()
    out1, _ = await run(b, index=0, repo=repo, thread="first")
    n = len(repo.list_llm_calls("s1"))

    class Dead(LLMBackend):
        name, model = "dead", "dead"

        async def generate(self, *a, **k):
            raise AssertionError("no model call expected")

    # same operation, no checkpoint (as if the process died after every node's commit): all steps come from the cache
    out2, _ = await run(Dead(), index=0, repo=repo, thread="second")
    assert out2["artifact"]["guide"] == out1["artifact"]["guide"] and len(repo.list_llm_calls("s1")) == n


# -- tools -----------------------------------------------------------------------------

async def test_tools_refuse_anything_outside_the_read_scope():
    scope = ReadingScope(quotes=QUOTES, current_index=1, guides={"0": {"guide": "前一段導讀"}, "2": {"guide": "未來草稿"}},
                         parts=[{"index": 0, "part_id": "part-01", "quote_from": 0, "quote_to": 2}],
                         compacts={"0": {"markdown": "筆記"}}, qa={"0": [{"question": "q", "answer": "a"}]})
    t = {x.name: x for x in build_tools(scope)}
    assert "前一段導讀" in await t["read_accepted_guide"].ainvoke({"unit_id": "u-a"})
    for name, args in [("read_accepted_guide", {"unit_id": "u-c"}), ("read_compact", {"part_id": "part-01"}),
                       ("read_source_units", {"unit_ids": ["other-range"]}), ("read_qa_history", {"unit_id": "u-c"})]:
        with pytest.raises(ScopeError):
            await t[name].ainvoke(args)
    with pytest.raises(Exception):
        await t["read_neighbor_units"].ainvoke({"unit_id": "u-b", "before": 2})
    later = await t["read_neighbor_units"].ainvoke({"unit_id": "u-b", "before": 0, "after": 1})
    assert "只供理解" in later


async def test_revision_keeps_the_chinese_text_it_was_not_asked_to_change():
    en = [{"id": "quote-01", "index": 0, "unit_id": "u-en", "label": "第 1 行", "heading": None,
           "text": "Gradient descent moves the parameters against the gradient, because the gradient points uphill."}]
    draft = {"chinese_text": "梯度下降沿梯度的反方向移動參數，因為梯度指向上升方向。", "guide": "梯度下降每一步都往梯度的反方向走一小步。",
             "key_points": [], "check": ""}
    revised = {**draft, "chinese_text": None, "guide": "梯度下降每一步都往梯度的反方向走一小步，因為梯度指向上升最快的方向。"}
    b = ByStep({"analyze": [PLAN], "draft": [draft],
                "review": [{"issues": [{"code": "missing_key_claim", "source_excerpt": "because the gradient points uphill",
                                        "suggested_fix": "補上原因"}]}, {"issues": []}], "revise": [revised]})
    pool = BackendPool(build=lambda c: b)
    deps = ReadingDeps(runtime_for=lambda sid: ModelRuntime(SNAP, pool), prompts_for=lambda sid: BUNDLE["prompts"],
                       scope_for=lambda sid, i: ReadingScope(quotes=en, current_index=i), sink=ListSink())
    task = ra.ReadingTaskInput(session_id="s1", unit_id="u-en", index=0, total=1, label="第 1 行", text=en[0]["text"],
                               material_title="ML", range_label="第 1 行", model_config_hash="h", prompt_bundle_hash="p")
    out = await invoke(build_reading_graph(deps), initial_state(task, op="op"), {"configurable": {"thread_id": "t"}})
    assert out["status"] == "accepted" and out["artifact"]["chinese_text"] == draft["chinese_text"]
    assert "chinese_text" in b.calls[3]["prompt"] and "必填" in b.calls[3]["prompt"]


async def test_extraction_check_withdraws_a_missing_claim_only_with_a_real_quote():
    """English source, Chinese draft: the reviewer's English citation cannot be matched by characters."""
    en = [{"id": "q", "index": 0, "unit_id": "u-en", "label": "第 1 行", "heading": None,
           "text": "The learning rate must be small enough; otherwise the loss may diverge."}]
    draft = {"chinese_text": "學習率必須夠小；否則損失可能發散。", "guide": "學習率必須夠小，否則損失可能會發散，這是使用梯度下降的條件。",
             "key_points": [], "check": ""}
    review = {"issues": [{"code": "missing_condition", "source_excerpt": "must be small enough", "suggested_fix": "補上條件"}]}

    async def go(verify_out):
        b = ByStep({"analyze": [PLAN], "draft": [draft], "review": [review, review], "revise": [draft], "verify": [verify_out]})
        pool = BackendPool(build=lambda c: b)
        deps = ReadingDeps(runtime_for=lambda sid: ModelRuntime(SNAP, pool), prompts_for=lambda sid: BUNDLE["prompts"],
                           scope_for=lambda sid, i: ReadingScope(quotes=en, current_index=i), sink=ListSink())
        task = ra.ReadingTaskInput(session_id="s1", unit_id="u-en", index=0, total=1, label="第 1 行", text=en[0]["text"],
                                   material_title="ML", range_label="第 1 行", model_config_hash="h", prompt_bundle_hash="p")
        return await invoke(build_reading_graph(deps), initial_state(task, op="op"), {"configurable": {"thread_id": "t"}}), b

    out, b = await go({"checks": [{"item": 1, "covered": True, "draft_quote": "學習率必須夠小，否則損失可能會發散"}]})
    assert out["status"] == "accepted" and b.steps() == ["analyze", "draft", "review", "revise", "review", "verify"]
    assert out["budget"]["calls"] == 6 and "核對" in out["artifact"]["warnings"][0]["detail"]
    out, _ = await go({"checks": [{"item": 1, "covered": True, "draft_quote": "學習率的上限由海森矩陣決定"}]})   # invented quote
    assert out["status"] == "failed"
