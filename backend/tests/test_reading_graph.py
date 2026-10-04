"""Guided-reading subgraph v2 (R3): Gate 1 (source claims against the full text) before any teaching text, approved
bridges, Gate 2 on the 主要正文 alone, bounded revisions and re-checks, one context packet per call, resume at the
failed node, replies and verified sources reused."""
import json
import re

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.db import Database
from app.llm.base import Capabilities, LLMBackend, LLMError, LLMResult
from app.llm.mock import MockBackend
from app.llm.runtime import BackendPool, ListSink, ModelRuntime, ModelSnapshot
from app.persistence.repository import DbSink, Repository
from app.reading import agent as ra
from app.reading.contracts import LearningSnapshot
from app.reading.gates import source_segments
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
EXCEPTION = {"id": "quote-04", "index": 3, "unit_id": "u-d", "label": "第 7 行", "heading": None,
             "text": "樣本平均數的分布會近似常態分布。除非樣本彼此不獨立，否則這個結論都成立。"}


def claim(i, sentences, statement, quote="", **kw):
    return {"id": f"C{i}", "sentences": sentences, "quote": quote, "statement": statement, "function": "claim", **kw}


def per_sentence(text: str, cn: str | None = None) -> dict:
    return {"chinese_text": cn, "claims": [claim(i, [sid], s) for i, (sid, s) in enumerate(source_segments(text), 1)],
            "needs_context": [], "limits": []}


def covered_audit(meta) -> dict:
    return {"claims": [{"id": c["id"], "verdict": "covered", "guide_quote": c["source_text"]} for c in meta["claims"]],
            "bridges": [{"id": b, "verdict": "ok", "guide_quote": ""} for b in meta.get("bridge_ids") or []],
            "additions": [], "uncovered_source": [], "coherence": "pass"}


def verbatim_body(meta) -> dict:
    return {"guide": "這一段的內容如下。\n\n" + "\n\n".join(c["source_text"] for c in meta["claims"]),
            "key_points": ["重點"], "check": "？", "bridges_used": [], "limits": []}


DEFAULTS = {
    "understand": lambda m: per_sentence(m["quote_text"]),
    "revise_source": lambda m: per_sentence(m["quote_text"]),
    "source_audit": lambda m: {"sentences": [{"id": s, "verdict": "faithful", "chinese_quote": ""} for s, _ in m["sentences"]],
                               "claims": [{"id": c, "verdict": "faithful"} for c in m["claim_ids"]]},
    "plan_bridges": lambda m: {"learning_goal": "讀懂", "bridges": []},
    "draft": verbatim_body, "revise_body": verbatim_body, "body_audit": covered_audit,
    "context": lambda m: {"action": "ready", "calls": [], "reason": ""},
    "verify": lambda m: {"checks": [{"item": 1, "verdict": "problem", "reason": "確實有問題"}]},
    "symbols": lambda m: {"formulas": ["\\mathbf{w} \\leftarrow \\mathbf{w} - \\eta \\nabla L"],
                          "symbols": [{"symbol": "w", "latex": "\\mathbf{w}", "style": "bold", "script": "none"}],
                          "unreadable": []},
}


class Scripted(LLMBackend):
    """Outputs per meta['step']: a scripted list is consumed first, then a sensible default derived from the
    request (claims quoting the source, a guide restating them, an audit quoting the guide). Records each call."""
    name, model = "scripted", "scripted"

    def __init__(self, script: dict | None = None, limit: int | None = None, vision: bool = False):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[dict] = []
        self.limit, self.vision = limit, vision

    async def capabilities(self):
        return Capabilities(context_limit=self.limit, vision=self.vision, source="test")

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
        step = (meta or {}).get("step")
        prompt = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        self.calls.append({"step": step, "prompt": prompt, "first": messages[0]["content"],
                           "images": len(messages[0].get("images") or [])})
        q = self.script.get(step)
        out = q.pop(0) if q else DEFAULTS[step](meta)
        if isinstance(out, Exception):
            raise out
        if callable(out):
            out = out(meta)
        return LLMResult(text=out if isinstance(out, str) else json.dumps(out, ensure_ascii=False), latency_ms=1)

    def steps(self):
        return [c["step"] for c in self.calls]

    def prompt(self, step: str, n: int = 0) -> str:
        return [c["first"] for c in self.calls if c["step"] == step][n]


def make(backend, *, repo=None, index=1, scope=None, quotes=None, session="s1", learning=None, kind="md", evidence=None,
         vsrc_cache=None, prompts=None):
    quotes = quotes or QUOTES
    pool = BackendPool(build=lambda c: backend)
    sink = DbSink(repo) if repo else ListSink()
    deps = ReadingDeps(runtime_for=lambda sid: ModelRuntime(SNAP, pool), prompts_for=lambda sid: prompts or BUNDLE["prompts"],
                       scope_for=lambda sid, i: scope or ReadingScope(quotes=quotes, current_index=i), sink=sink, repo=repo,
                       vsrc_cache=vsrc_cache, render_region=lambda path, page, bbox: b"\x89PNG-stub")
    q = quotes[index]
    task = ra.ReadingTaskInput(session_id=session, unit_id=q["unit_id"], index=index, total=len(quotes), label=q["label"],
                               text=q["text"], block_ids=[f"b{index}"], material_title="微積分", range_label="第 1–5 行",
                               model_config_hash=SNAP.config_hash, prompt_bundle_hash=BUNDLE["hash"],
                               learning=learning or LearningSnapshot(), source_kind=kind, symbol_evidence=evidence)
    return deps, task, sink


async def run(backend, *, index=1, repo=None, checkpointer=None, thread="t", op=None, **kw):
    deps, task, sink = make(backend, repo=repo, index=index, **kw)
    g = build_reading_graph(deps, checkpointer)
    out = await invoke(g, initial_state(task, op=op or f"{task.session_id}:c1:guide:{task.unit_id}:1"),
                       {"configurable": {"thread_id": thread}})
    return out, sink


# -- happy path ------------------------------------------------------------------------------------

async def test_mock_guide_passes_both_gates_in_four_calls():
    out, sink = await run(MockBackend(), index=0)
    assert out["status"] == "accepted" and out["budget"]["calls"] == 4
    assert [r.node for r in sink.records] == ["understand", "plan_bridges", "draft", "body_audit"]
    a = out["artifact"]
    assert a["gate1"]["status"] == "pass" and a["gate2"]["status"] == "pass"
    assert a["source_claims"] and a["source_refs"] == ["b0"] and a["review_status"] == "passed"
    assert a["workflow_version"] == ra.READING_WORKFLOW_VERSION and a["prompt_version"] == BUNDLE["hash"]
    for r in sink.records:   # every call records its budget and packet
        assert r.request["context_budget"]["fits"] and r.request["packet"]["node"] == r.node


# -- Gate 1 ---------------------------------------------------------------------------------------

async def test_gate1_finds_an_exception_the_analysis_left_out_by_reading_the_full_text():
    """The claims list is checked against the source, not against itself (matrix: 分析漏列原文例外)."""
    quotes = QUOTES + [EXCEPTION]
    # S2 is cited only through a partial quote that leaves "除非樣本彼此不獨立" out
    incomplete = {"chinese_text": None, "claims": [claim(1, ["S1"], "樣本平均數的分布會近似常態分布。"),
                                                   claim(2, ["S2"], "這個結論都成立", quote="否則這個結論都成立")],
                  "needs_context": []}
    b = Scripted({"understand": [incomplete]})
    out, _ = await run(b, index=3, quotes=quotes)
    assert b.steps()[:2] == ["understand", "revise_source"]
    issues = b.prompt("revise_source")
    assert "除非" in issues and "coverage" in issues and "部分涵蓋" in issues
    assert out["status"] == "accepted"

    b = Scripted({"understand": [incomplete], "revise_source": [incomplete]})
    out, _ = await run(b, index=3, quotes=quotes)
    assert out["status"] == "failed" and out["error"]["code"] == "source_unverified"
    assert "draft" not in b.steps() and "plan_bridges" not in b.steps()   # no teaching text before Gate 1


async def test_gate1_requires_real_sentence_ids_and_verbatim_quotes():
    for bad in (claim(1, ["S1"], "需要連續且可微", quote="使用均值定理需要連續且可微"),   # quote not in S1
                claim(1, ["S7"], "需要連續且可微")):                                       # no such sentence
        b = Scripted({"understand": [{"chinese_text": None, "claims": [bad]}]})
        out, _ = await run(b, index=0)
        assert "anchor" in b.prompt("revise_source") and out["status"] == "accepted"


async def test_gate1_settles_strength_flags_with_an_audit_or_a_revision():
    """A claim whose statement drops the source's negation is flagged by the program; for a Chinese source the
    audit is only called for flagged claims, and a "faithful" verdict cannot fix a missing anchor."""
    weakened = {"chinese_text": None, "claims": [claim(1, ["S1"], "若任一條件成立，這樣的 c 就存在。")]}
    b = Scripted({"understand": [weakened], "source_audit": [{"sentences": [], "claims": [{"id": "C1", "verdict": "distorted",
                                                                                           "note": "否定被拿掉"}]}]})
    out, _ = await run(b, index=2)
    assert b.steps()[:3] == ["understand", "source_audit", "revise_source"] and out["status"] == "accepted"


async def test_non_chinese_source_needs_a_sentence_aligned_translation_and_its_audit():
    en = [{"id": "q", "index": 0, "unit_id": "u-en", "label": "第 1 行", "heading": None,
           "text": "The learning rate must be small enough. Otherwise the loss may diverge."}]
    tr = [{"id": "S1", "zh": "學習率必須夠小。"}, {"id": "S2", "zh": "否則損失可能發散。"}]
    und = {"translations": tr, "claims": [claim(1, ["S1"], ""), claim(2, ["S2"], "")]}
    missing_one = {**und, "translations": tr[:1]}
    b = Scripted({"understand": [missing_one], "revise_source": [missing_one]})
    out, _ = await run(b, index=0, quotes=en)
    assert out["status"] == "failed" and out["error"]["code"] == "source_unverified" and "S2 沒有中文課文" in out["error"]["detail"]
    distorted = {"sentences": [{"id": "S1", "verdict": "faithful"}, {"id": "S2", "verdict": "distorted", "note": "may 被譯成必然"}],
                 "claims": []}
    b = Scripted({"understand": [und, und], "revise_source": [und], "source_audit": [distorted, distorted]})
    out, _ = await run(b, index=0, quotes=en)
    assert out["status"] == "failed" and "改變原意" in out["error"]["detail"]
    b = Scripted({"understand": [und]})
    out, _ = await run(b, index=0, quotes=en)
    a = out["artifact"]
    assert out["status"] == "accepted" and a["chinese_text"] == "學習率必須夠小。否則損失可能發散。"
    assert [c["statement"] for c in a["source_claims"]] == ["學習率必須夠小。", "否則損失可能發散。"]   # resolved
    assert b.steps() == ["understand", "source_audit", "plan_bridges", "draft", "body_audit"]
    assert "學習率必須夠小" not in b.prompt("body_audit").split("## 主要正文")[0].split("## 命題")[0]   # no 中文課文 in Gate 2's source part


async def test_a_translation_that_drops_a_formula_or_flips_a_negation_is_caught_by_the_program():
    en = [{"id": "q", "index": 0, "unit_id": "u-en", "label": "第 1 行", "heading": None,
           "text": "Only if $x > 0$ is the formula $$y = 1/x$$ valid."}]
    no_formula = {"translations": [{"id": "S1", "zh": "只有在 x 為正時，這個公式才成立。"}], "claims": [claim(1, ["S1"], "")]}
    b = Scripted({"understand": [no_formula], "revise_source": [no_formula]})
    out, _ = await run(b, index=0, quotes=en)
    assert out["status"] == "failed" and "沒有原樣保留公式" in out["error"]["detail"]


PDF_UNIT = [{"id": "q", "index": 0, "unit_id": "u-p", "label": "第 1 頁", "heading": None, "text": "權重向量 w 依 w ← w − η∇L 更新。"}]


def needs_visual(**clues):
    return {"status": "needs_visual", "version": "pdf-evidence-v1", "pdf_path": "/x.pdf", "regions": [{"page": 1, "bbox": [0, 0, 10, 10]}],
            "clues": {"bold_tokens": ["w"], **clues}, "reason": "PDF 文字層無法確認符號的字型"}


async def test_pdf_symbols_without_evidence_or_vision_hold_before_any_call():
    b = Scripted()
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf")                       # no layout evidence at all
    assert out["status"] == "failed" and out["error"]["code"] == "notation_unverified" and b.calls == []
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual())   # text-only model
    assert out["status"] == "failed" and "不支援影像輸入" in out["error"]["detail"] and b.calls == []


async def test_pdf_symbols_are_read_from_rendered_regions_and_bind_the_notation():
    b = Scripted(vision=True)
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual())
    assert b.steps()[0] == "symbols" and b.calls[0]["images"] == 1
    assert "\\mathbf{w}" in b.prompt("understand")                                     # the evidence steers the source stage
    assert out["status"] == "accepted" and out["symbol_evidence"]["status"] == "confirmed"
    assert out["artifact"]["gate1"]["status"] == "pass"
    # a transcription that contradicts the font clues (w bold in the font, plain in the reading) is not evidence
    wrong = {"formulas": ["w \\leftarrow w - \\eta \\nabla L"], "symbols": [{"symbol": "w", "latex": "w", "style": "italic"}]}
    b = Scripted({"symbols": [wrong]}, vision=True)
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual())
    assert out["status"] == "failed" and out["error"]["code"] == "notation_unverified" and "粗體" in out["error"]["detail"]
    assert b.steps() == ["symbols"]


async def test_claims_that_flatten_a_bold_vector_go_back_for_revision():
    flat = {"chinese_text": None, "claims": [claim(1, ["S1"], "權重向量 $w$ 依 $w \\leftarrow w - \\eta \\nabla L$ 更新。")]}
    b = Scripted({"understand": [flat]}, vision=True)
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual())
    assert "revise_source" in b.steps() and "粗體" in b.prompt("revise_source") and out["status"] == "accepted"


# -- bridges and Gate 2 ------------------------------------------------------------------------------

async def test_bridges_must_target_real_claims_and_carry_subject_matter_only():
    bad = {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C9"],
                                             "content": "依照你的要求補充可微的定義", "basis": "prerequisite"}]}
    ok = {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"],
                                            "content": "可微表示函數在該點有切線斜率。", "basis": "prerequisite"}]}
    b = Scripted({"plan_bridges": [bad, ok]})
    out, _ = await run(b, index=0)
    assert b.steps().count("plan_bridges") == 2 and "C9" in b.calls[2]["prompt"] and "產製" in b.calls[2]["prompt"]
    assert "可微表示函數在該點有切線斜率" in b.prompt("draft") and "不熟悉可微" not in b.prompt("draft")
    assert out["status"] == "accepted" and out["artifact"]["bridges"]["bridges"][0]["id"] == "B1"


async def test_unapproved_supplement_and_missing_claims_go_back_for_revision():
    def with_supplement(m):
        d = verbatim_body(m)
        d["guide"] += "\n\n補充說明：只要函數連續就一定可以使用均值定理。"
        return d
    b = Scripted({"draft": [with_supplement]})
    out, _ = await run(b, index=0)
    assert b.steps() == ["understand", "plan_bridges", "draft", "revise_body", "body_audit"]
    assert "unapproved_supplement" in b.prompt("revise_body") and out["status"] == "accepted"

    missing = lambda m: {**covered_audit(m), "claims": [{"id": "C1", "verdict": "missing", "note": "沒寫到可微"}]}
    b = Scripted({"body_audit": [missing, missing], "verify": [{"checks": [{"item": 1, "verdict": "no_problem",
                                                                            "guide_quote": "函數在開區間內可微"}]}]})
    out, _ = await run(b, index=0)
    assert b.steps() == ["understand", "plan_bridges", "draft", "body_audit", "revise_body", "body_audit", "verify"]
    assert out["status"] == "failed" and out["error"]["code"] == "quality_check_failed"   # invented quote


async def test_a_covered_verdict_counts_only_with_a_quote_from_the_guide():
    def from_chinese(m):
        a = covered_audit(m)
        a["claims"][0]["guide_quote"] = "這句只在中文課文裡"
        return a
    b = Scripted({"body_audit": [from_chinese], "verify": [
        {"checks": [{"item": 1, "verdict": "no_problem", "guide_quote": QUOTES[0]["text"]}]}]})
    out, _ = await run(b, index=0)
    assert b.steps()[-1] == "verify" and out["status"] == "accepted"
    assert [w["code"] for w in out["artifact"]["warnings"]] == ["review_withdrawn"]


async def test_an_empty_or_partial_audit_is_never_a_pass():
    b = Scripted({"body_audit": [{}, {"claims": [], "bridges": [], "additions": [], "uncovered_source": [], "coherence": "pass"}]})
    out, _ = await run(b, index=0)
    assert out["status"] == "failed" and out["error"]["code"] == "review_invalid" and b.steps().count("body_audit") == 2


async def test_auditor_found_source_text_beyond_the_claims_blocks():
    beyond = lambda m: {**covered_audit(m), "uncovered_source": ["並在開區間內可微"]}
    invented = lambda m: {**covered_audit(m), "uncovered_source": ["函數必須有界"]}
    b = Scripted({"body_audit": [beyond]})
    out, _ = await run(b, index=0)
    assert "revise_body" in b.steps()
    b = Scripted({"body_audit": [invented]})
    out, _ = await run(b, index=0)
    assert out["status"] == "accepted" and "review_citation_invalid" in [w["code"] for w in out["artifact"]["warnings"]]


# -- context ---------------------------------------------------------------------------------------

async def test_referring_unit_gets_its_neighbour_from_the_program_and_every_stage_sees_it():
    b = Scripted({"body_audit": [lambda m: {**covered_audit(m), "claims": [{"id": "C1", "verdict": "missing"}]}]})
    out, _ = await run(b, index=1)
    assert "context" not in b.steps()                       # no model call to find an obvious referent
    sup = out["supplements"][0]
    assert sup["by"] == "program" and sup["source_ids"] == ["u-a"] and sup["complete"]
    for step in ("understand", "draft", "body_audit", "revise_body"):
        assert "閉區間上連續" in b.prompt(step), step       # same supplement in every stage
    assert out["artifact"]["source_refs"] == ["b1", "u-a"] and out["artifact"]["context_status"] == "provided"


async def test_model_context_redoes_the_understanding_with_what_it_read():
    hinting = {**per_sentence(QUOTES[2]["text"]), "needs_context": ["「任一條件」指前一段的兩個條件"]}
    read = {"action": "read", "reason": "前一段",
            "calls": [{"tool": "read_source_units", "args": {"unit_ids": ["u-a"]}}]}
    b = Scripted({"understand": [hinting], "context": [read, {"action": "ready", "calls": [], "reason": ""}]})
    out, _ = await run(b, index=2)
    assert b.steps()[:4] == ["understand", "context", "context", "understand"]
    assert "閉區間上連續" in b.prompt("understand", 1) and out["status"] == "accepted"
    assert out["artifact"]["source_refs"] == ["b2", "u-a"]


async def test_a_unit_that_cannot_fit_stops_without_calling_the_model():
    big = [{"id": "q", "index": 0, "unit_id": "u-big", "label": "第 1 行", "heading": None, "text": "長" * 9000}]
    b = Scripted(limit=8192)
    out, _ = await run(b, index=0, quotes=big)
    assert out["status"] == "failed" and out["error"]["code"] == "context_overflow" and b.calls == []


async def test_optional_reader_context_is_dropped_before_anything_required():
    learning = LearningSnapshot(read_claims=[{"unit_id": f"u{i}", "claim_id": "C1", "statement": "前" * 400} for i in range(12)])
    b = Scripted(limit=6000)
    out, sink = await run(b, index=0, learning=learning)
    pb = next(r for r in sink.records if r.node == "plan_bridges").request["packet"]
    assert "read_claims" in pb["dropped"] and all(i["included"] for i in pb["items"] if i["required"])
    assert out["status"] == "accepted"


# -- budget, failures, resume, caches ------------------------------------------------------------------

async def test_invalid_output_twice_fails_within_budget():
    b = Scripted({"understand": ["nope", "still nope"]})
    out, _ = await run(b)
    assert out["status"] == "failed" and out["error"]["code"] == "invalid_model_output" and len(b.calls) == 2


async def test_unavailable_model_stops_at_that_node_and_resume_continues_there(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite3"))
    b = Scripted({"draft": [LLMError("Ollama 連線失敗")]})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:
        with pytest.raises(ReadingUnavailable):
            await run(b, repo=repo, checkpointer=cp)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:   # a fresh saver = restart
        deps, task, _ = make(b, repo=repo)
        out = await invoke(build_reading_graph(deps, cp), None, {"configurable": {"thread_id": "t"}})
    assert out["status"] == "accepted"
    assert b.steps() == ["understand", "plan_bridges", "draft", "draft", "body_audit"]   # nothing before repeated
    assert out["budget"]["calls"] == 5   # the failed call still counts
    calls = repo.list_llm_calls("s1")
    assert calls[2]["error_kind"] == "unavailable" and calls[3]["validated"]["guide"]


async def test_replies_committed_before_a_lost_checkpoint_are_reused(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite3"))
    out1, _ = await run(MockBackend(), index=0, repo=repo, thread="first")
    n = len(repo.list_llm_calls("s1"))

    class Dead(LLMBackend):
        name, model = "dead", "dead"

        async def generate(self, *a, **k):
            raise AssertionError("no model call expected")

    out2, _ = await run(Dead(), index=0, repo=repo, thread="second")
    assert out2["artifact"]["guide"] == out1["artifact"]["guide"] and len(repo.list_llm_calls("s1")) == n


async def test_a_verified_source_is_reused_by_another_session_but_teaching_is_not(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite3"))
    b = Scripted()
    out1, _ = await run(b, index=0, repo=repo, session="s1")
    out2, _ = await run(b, index=0, repo=repo, session="s2", learning=LearningSnapshot(background="高中數學"))
    assert b.steps().count("understand") == 1 and b.steps().count("draft") == 2
    assert out2["artifact"]["verified_source_key"] == out1["artifact"]["verified_source_key"]
    assert out2["artifact"]["learning_hash"] != out1["artifact"]["learning_hash"]
    b2 = Scripted()
    out3, _ = await run(b2, index=0, repo=repo, session="s3", quotes=[{**QUOTES[0], "text": QUOTES[0]["text"] + "這是新增的一句。"}])
    assert b2.steps()[0] == "understand"   # changed source → its own verification


async def test_dependency_changes_invalidate_only_what_depends_on_them():
    """Source-stage prompts and symbol evidence are inputs of the verified source; a 主要正文 prompt or the reader
    are not (WaAi-ML typed invalidation: A/B/C vs D/E)."""
    cache: dict = {}
    b = Scripted(vision=True)
    await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual(), vsrc_cache=cache)
    body_only = {**BUNDLE["prompts"], "reading_body": BUNDLE["prompts"]["reading_body"] + "\n（格式微調）"}
    await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual(), vsrc_cache=cache, prompts=body_only, session="s2")
    assert b.steps().count("understand") == 1 and b.steps().count("symbols") == 1 and b.steps().count("draft") == 2
    await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual(italic_tokens=["L"]), vsrc_cache=cache, session="s3")
    assert b.steps().count("understand") == 2      # new symbol evidence → the source is verified again
    source_prompt = {**BUNDLE["prompts"], "reading_understand": BUNDLE["prompts"]["reading_understand"] + "\n（新規則）"}
    await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual(), vsrc_cache=cache, prompts=source_prompt, session="s4")
    assert b.steps().count("understand") == 3


async def test_budget_is_never_exceeded():
    always_missing = lambda m: {**covered_audit(m), "claims": [{"id": c["id"], "verdict": "missing"} for c in m["claims"]]}
    b = Scripted({"body_audit": [always_missing] * 5})
    out, _ = await run(b, index=0)
    assert out["status"] == "failed" and out["budget"]["calls"] <= out["budget"]["max_calls"]
    assert b.steps().count("revise_body") == 1


# -- tools -----------------------------------------------------------------------------------------

async def test_tools_refuse_anything_outside_the_read_scope():
    scope = ReadingScope(quotes=QUOTES, current_index=1, guides={"0": {"guide": "前一段導讀"}, "2": {"guide": "未來草稿"}},
                         parts=[{"index": 0, "part_id": "part-01", "quote_from": 0, "quote_to": 2}],
                         compacts={"0": {"markdown": "筆記"}}, qa={"0": [{"question": "q", "answer": "a"}]})
    t = {x.name: x for x in build_tools(scope)}
    assert "前一段導讀" in (await t["read_accepted_guide"].ainvoke({"unit_id": "u-a"})).text
    for name, args in [("read_accepted_guide", {"unit_id": "u-c"}), ("read_compact", {"part_id": "part-01"}),
                       ("read_source_units", {"unit_ids": ["other-range"]}), ("read_qa_history", {"unit_id": "u-c"})]:
        with pytest.raises(ScopeError):
            await t[name].ainvoke(args)
    with pytest.raises(Exception):
        await t["read_neighbor_units"].ainvoke({"unit_id": "u-b", "before": 2})
    later = await t["read_neighbor_units"].ainvoke({"unit_id": "u-b", "before": 0, "after": 1})
    assert "只供理解" in later.text and later.source_ids == ["u-c"] and later.complete


def test_audit_schemas_name_every_expected_id():
    from app.reading.contracts import BodyAudit
    from app.reading.gates import exact_ids_schema
    sch = exact_ids_schema(BodyAudit, {"claims": ["C1", "C2"], "bridges": []})
    claims = sch["properties"]["claims"]
    assert claims["minItems"] == claims["maxItems"] == 2 and claims["items"]["properties"]["id"]["enum"] == ["C1", "C2"]
    assert sch["properties"]["bridges"]["maxItems"] == 0
    assert set(sch["required"]) >= {"claims", "bridges", "additions", "uncovered_source", "coherence"}


async def test_a_supplement_paragraph_must_carry_an_approved_bridge():
    bridge = {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"], "boundary": "supplement",
                                                "content": "可微表示函數在該點有切線斜率。", "basis": "prerequisite"}]}

    def body(extra):
        def make(m):
            d = verbatim_body(m)
            d["guide"] += f"\n\n**補充說明：**{extra}"
            return d
        return make
    b = Scripted({"plan_bridges": [bridge], "draft": [body("可微表示函數在每一點都有切線斜率。")]})
    out, _ = await run(b, index=0)
    assert "revise_body" not in b.steps() and out["status"] == "accepted"          # the approved bridge, reworded
    b = Scripted({"plan_bridges": [bridge], "draft": [body("三次握手確保雙方建立可靠的連線。")]})
    out, _ = await run(b, index=0)
    assert "unapproved_supplement" in b.prompt("revise_body")                     # content no bridge approved
