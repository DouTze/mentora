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


def reworded(text: str) -> str:
    """How the scripted drafter restates a claim: not verbatim (so coverage goes through the auditor), same meaning."""
    body = text.rstrip("。.！？!?")
    return body[:-1] + "確實" + body[-1:] + text[len(body):] if len(body) > 1 else "確實" + text


SUPPORT = {"source": "S1", "derivation": "由 S1 的條件代入定義，可得這個結果",
           "prerequisite": "切線斜率是導數的幾何意義，微積分入門課程都會教"}


def dropped(out, content="可微表示函數在該點有切線斜率") -> bool:
    """Published from the source alone: the unverifiable bridge is recorded as dropped and not in the guide."""
    a = out["artifact"]
    return (out["status"] == "accepted" and [b["id"] for b in a["bridges_dropped"]] == ["B1"] and not a["bridges"]["bridges"]
            and content not in a["guide"] and "教學補充無法核對，這段只依原文導讀" in a["limits"])


def covered_audit(meta) -> dict:
    return {"claims": [{"id": c["id"], "verdict": "covered", "guide_quote": reworded(c["source_text"])} for c in meta["claims"]],
            "bridges": [{"id": b["id"], "verdict": "ok", "correct": "yes", "support": SUPPORT.get(b["basis"], "S1"),
                         "guide_quote": b["content"]} for b in meta.get("bridges") or []],
            "additions": [], "uncovered_source": [], "coherence": "pass"}


def verbatim_body(meta) -> dict:
    return {"guide": "這一段的內容如下。\n\n" + "\n\n".join(reworded(c["source_text"]) for c in meta["claims"])
                     + "".join(f"\n\n$${f}$$" for f in meta.get("evidence_formulas") or [])
                     + "".join(f"\n\n{b['content']}" for b in meta.get("bridges") or []),
            "key_points": ["重點"], "check": "？", "bridges_used": [], "limits": []}


def with_evidence(u: dict, m: dict) -> dict:
    if m.get("evidence_formulas"):   # a faithful model writes the formulas read from the PDF region
        u["claims"][0]["formulas"] = list(m["evidence_formulas"])
    return u


DEFAULTS = {
    "understand": lambda m: with_evidence(per_sentence(m["quote_text"]), m),
    "revise_source": lambda m: with_evidence(per_sentence(m["quote_text"]), m),
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
    b = Scripted({"body_audit": [from_chinese]})
    out, _ = await run(b, index=0)
    # the program finds the claim in the guide itself: published, with the unconfirmed restatement shown to the reader
    a = out["artifact"]
    assert out["status"] == "accepted" and "verify" not in b.steps() and "revise_body" not in b.steps()
    assert [w["code"] for w in a["warnings"]] == ["coverage_unconfirmed"]
    assert [n["kind"] for n in a["unverified"]] == ["coverage"] and a["unverified"][0]["guide"] in a["guide"]

    # the guide really lacks it: a Chinese-text quote never answers for the guide — a program fact, revised then held
    gone = lambda m: {**verbatim_body(m), "guide": "這一段在講別的事情，和原文無關。"}
    b = Scripted({"body_audit": [from_chinese, from_chinese], "draft": [gone], "revise_body": [gone]})
    out, _ = await run(b, index=0)
    assert out["status"] == "failed" and "verify" not in b.steps()
    assert any(f["code"] == "missing_key_claim" and f["severity"] == "major" for f in out["findings"])


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


async def test_a_run_keeps_its_input_so_it_can_be_replayed(tmp_path):
    """scripts/gate_cases.py replays a run from its stored input and its recorded calls (same operation key)."""
    repo = Repository(Database(tmp_path / "db.sqlite3"))
    out, _ = await run(MockBackend(), index=0, repo=repo)
    [inp] = repo.list_artifacts("s1", "guide_input")
    s0 = inp["payload"]
    assert s0["task"]["unit_id"] == QUOTES[0]["unit_id"] and s0["draft"] is None
    assert all(c["operation_key"].startswith(s0["op"] + ":") for c in repo.list_llm_calls("s1"))
    deps, _, _ = make(MockBackend(), index=0)
    again = await invoke(build_reading_graph(deps), s0, {"configurable": {"thread_id": "replay"}})
    assert again["status"] == out["status"] == "accepted" and again["artifact"]["guide"] == out["artifact"]["guide"]


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
    # free text is bounded: a reason repeated until the token limit (qwen3-vl:4b) can no longer break the JSON
    claim = claims["items"]["properties"]
    assert claim["note"]["maxLength"] == claim["guide_quote"]["maxLength"] == 400 and "maxLength" not in claim["id"]
    assert sch["properties"]["uncovered_source"]["items"]["maxLength"] == 400


def test_verify_answers_are_bounded_and_always_fit_the_output_budget():
    from app.reading import gates
    sch = gates.verify_schema(3)
    check = sch["$defs"]["VerifyCheck"]["properties"]
    assert check["item"]["enum"] == [1, 2, 3] and sch["properties"]["checks"]["maxItems"] == 3
    assert (check["guide_quote"]["maxLength"], check["reason"]["maxLength"]) == (200, 300)
    for n in (1, 2, 3, 5):   # every check at its bounds fits, and the budget never shrinks below the old one
        assert gates.verify_tokens(n) >= 128 + n * (200 + 300 + 80) and gates.verify_tokens(n) >= 512 + 320 * n


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


async def test_a_rewritten_claim_is_audited_before_it_can_be_cached():
    """Review counterexample: a cited sentence proves where a claim comes from, not that it is understood. "長乘以寬"
    restated as "長加上寬" must not become a verified source; the cited text itself takes the fast path."""
    area = [{"id": "q", "index": 0, "unit_id": "u-area", "label": "第 1 行", "heading": None, "text": "矩形面積等於長乘以寬。"}]
    wrong = {"chinese_text": None, "claims": [claim(1, ["S1"], "矩形面積等於長加上寬。")]}
    distorted = {"sentences": [], "claims": [{"id": "C1", "verdict": "distorted", "note": "乘被改成加"}]}
    cache: dict = {}
    b = Scripted({"understand": [wrong], "revise_source": [wrong], "source_audit": [distorted, distorted]})
    out, _ = await run(b, index=0, quotes=area, vsrc_cache=cache)
    assert b.steps() == ["understand", "source_audit", "revise_source", "source_audit"]
    assert out["status"] == "failed" and out["error"]["code"] == "source_unverified" and not cache
    b = Scripted({"understand": [wrong]})                        # the audit (default) judges it faithful: allowed
    out, _ = await run(b, index=0, quotes=area)
    assert b.steps()[:2] == ["understand", "source_audit"] and out["status"] == "accepted"
    verbatim = {"chinese_text": None, "claims": [claim(1, ["S1"], "")]}
    b = Scripted({"understand": [verbatim]})
    out, _ = await run(b, index=0, quotes=area)
    assert "source_audit" not in b.steps() and out["status"] == "accepted"
    added = {"chinese_text": None, "claims": [claim(1, ["S1"], "", conditions=["長與寬都是正數"])]}
    b = Scripted({"understand": [added]})
    await run(b, index=0, quotes=area)
    assert "source_audit" in b.steps()                          # an added condition is checked too


async def test_formulas_read_from_the_pdf_region_must_reach_the_claims_and_the_guide():
    """Review counterexample: confirmed symbol evidence, but claims and guide drop the formula entirely."""
    no_formula = {"chinese_text": None, "claims": [claim(1, ["S1"], "")]}
    b = Scripted({"understand": [no_formula], "revise_source": [no_formula]}, vision=True)
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual())
    assert out["status"] == "failed" and "依 PDF 版面取證的公式" in out["error"]["detail"]
    assert "draft" not in b.steps()

    prose = lambda m: {"guide": "權重向量會沿梯度的反方向更新，學習率決定每一步的大小。", "key_points": [], "check": "",
                       "bridges_used": []}
    b = Scripted({"draft": [prose], "revise_body": [prose]}, vision=True)
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual())
    assert out["status"] == "failed" and "依 PDF 版面取證的" in out["error"]["detail"]
    assert "\\mathbf{w} \\leftarrow" in b.prompt("draft") and "\\mathbf{w} \\leftarrow" in b.prompt("revise_body")
    b = Scripted(vision=True)
    out, _ = await run(b, index=0, quotes=PDF_UNIT, kind="pdf", evidence=needs_visual())
    assert out["status"] == "accepted" and "\\mathbf{w} \\leftarrow" in b.prompt("body_audit")


async def test_an_ok_bridge_needs_a_real_guide_passage_and_a_supported_correctness_judgement():
    """Review counterexample: the bridge never reaches the guide, the audit says ok with an empty quote."""
    bridge = {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"],
                                                "content": "可微表示函數在該點有切線斜率。", "basis": "source"}]}
    no_bridge_body = lambda m: {**verbatim_body({**m, "bridges": []}), "bridges_used": ["B1"]}

    def audit(**b1):
        return lambda m: {**covered_audit({**m, "bridges": []}),
                          "bridges": [{"id": "B1", "verdict": "ok", "correct": "yes", "support": "S1", "guide_quote": "", **b1}]}
    b = Scripted({"plan_bridges": [bridge], "draft": [no_bridge_body], "revise_body": [no_bridge_body],
                  "body_audit": [audit(), audit()]})
    out, _ = await run(b, index=0)
    # nothing of the bridge is on the page, so nothing can mislead: published as written, not rewritten; the record
    # keeps the doubt and does not claim the bridge was used
    a = out["artifact"]
    assert out["status"] == "accepted" and "revise_body" not in b.steps() and "切線斜率" not in a["guide"]
    assert a["bridges_used"] == [] and [w["code"] for w in a["warnings"]] == ["bridge_unconfirmed"]

    b = Scripted({"plan_bridges": [bridge], "body_audit": [audit(guide_quote="可微表示函數在該點有切線斜率", correct="no",
                                                                note="可微不等於只有切線")] * 2})
    out, _ = await run(b, index=0)
    assert dropped(out) and any("內容不正確" in json.dumps(h.get("findings"), ensure_ascii=False) for h in out["history"])

    b = Scripted({"plan_bridges": [bridge], "body_audit": [audit(guide_quote="可微表示函數在該點有切線斜率", support="S9")]})
    out, _ = await run(b, index=0)
    # a source-based bridge citing no real sentence is not "correct": re-checked (the re-check confirms the doubt),
    # revised, and accepted only when the next audit gives real support
    steps = b.steps()
    assert steps.index("verify") < steps.index("revise_body") and out["status"] == "accepted"



async def test_claims_the_guide_writes_verbatim_are_covered_by_the_program_not_the_auditor():
    """Cost: a claim whose whole cited text is in the guide, formulas included, needs no auditor verdict."""
    def mixed(m):
        cs = m["claims"]
        return {"guide": cs[0]["source_text"] + "\n\n" + "\n\n".join(reworded(c["source_text"]) for c in cs[1:]),
                "key_points": [], "check": "", "bridges_used": []}
    quotes = QUOTES + [EXCEPTION]
    b = Scripted({"draft": [mixed]})
    out, _ = await run(b, index=3, quotes=quotes)
    audit_prompt = b.prompt("body_audit")
    assert "- C1［" not in audit_prompt and "- C2［" in audit_prompt
    assert out["status"] == "accepted" and out["audit"]["program_covered"] == ["C1"]
    assert out["artifact"]["gate2"]["status"] == "pass"


# -- second review: stale caches, "same text" that is not, bridge support on re-check ----------------------------

AREA = [{"id": "q", "index": 0, "unit_id": "u-area", "label": "第 1 行", "heading": None, "text": "矩形面積等於長乘以寬。"}]


async def test_a_source_verified_under_older_gate_rules_is_verified_again(monkeypatch):
    """Review counterexample: a "長加上寬" verification cached by the looser rules was reused by the new code, which
    skipped understand and source_audit and accepted it. A cached verification counts only under the same rules."""
    from app.reading import gates
    cache: dict = {}
    await run(Scripted(), index=0, quotes=AREA, vsrc_cache=cache)
    (key, entry), = cache.items()
    assert entry["verifier_version"] == gates.GATE_RULES_VERSION
    stale = {**entry, "claims": [{**entry["claims"][0], "statement": "矩形面積等於長加上寬。"}]}
    del stale["verifier_version"]                              # what the older code wrote
    cache[key] = stale
    wrong = {"chinese_text": None, "claims": [claim(1, ["S1"], "矩形面積等於長加上寬。")]}
    distorted = {"sentences": [], "claims": [{"id": "C1", "verdict": "distorted", "note": "乘被改成加"}]}
    b = Scripted({"understand": [wrong], "revise_source": [wrong], "source_audit": [distorted, distorted]})
    out, _ = await run(b, index=0, quotes=AREA, vsrc_cache=cache, session="s2")
    assert b.steps()[:2] == ["understand", "source_audit"] and out["status"] == "failed"

    cache[key] = {**entry, "verifier_version": "gate1-older"}  # same key, other rules: not reused either
    b = Scripted()
    await run(b, index=0, quotes=AREA, vsrc_cache=cache, session="s3")
    assert b.steps()[0] == "understand"

    monkeypatch.setattr(gates, "GATE_RULES_VERSION", "gate1-next")   # changed rules → another key
    b = Scripted()
    await run(b, index=0, quotes=AREA, vsrc_cache=cache, session="s4")
    assert b.steps()[0] == "understand" and len(cache) == 2


def test_gate_rules_version_follows_the_acceptance_code():
    from app.reading import gates
    assert gates.GATE_RULES_VERSION.startswith(gates.GATE_RULES_LABEL + "+")


async def test_only_provably_lossless_differences_count_as_the_same_text():
    """Review counterexamples: punctuation stripping turned 1.5 into 15, .5 into 5 and 2**3 into 23, so a changed
    claim passed Gate 1 as the cited text itself. Only whitespace and the final sentence mark are ignored now."""
    from app.reading.gates import _plain
    for a, b in [("平均值是 1.5 公尺。", "平均值是 15 公尺。"), ("長度是 .5 公尺", "長度是 5 公尺"), ("結果是 2**3", "結果是 23"),
                 ("共 1,000 人", "共 1000 人"), ("比例 1:2", "比例 12"), ("他不，是學生", "他不是學生"), ("「長」乘以寬", "長乘以寬"),
                 ("x²", "x2"), ("f(a, b)", "f(ab)")]:
        assert _plain(a) != _plain(b), (a, b)
    assert _plain("矩形面積 等於長乘以寬。") == _plain("矩形面積等於長乘以寬") and _plain("a  b.") == _plain("a b")

    for src, changed in [("平均值是 1.5 公尺。", "平均值是 15 公尺。"), ("長度是 .5 公尺。", "長度是 5 公尺。"),
                         ("程式印出 `2**3` 的值。", "程式印出 `23` 的值。")]:
        q = [{"id": "q", "index": 0, "unit_id": "u-n", "label": "第 1 行", "heading": None, "text": src}]
        b = Scripted({"understand": [{"chinese_text": None, "claims": [claim(1, ["S1"], changed)]}]})
        await run(b, index=0, quotes=q)
        assert "source_audit" in b.steps(), changed
        b = Scripted({"understand": [{"chinese_text": None, "claims": [claim(1, ["S1"], src)]}]})
        await run(b, index=0, quotes=q)
        assert "source_audit" not in b.steps(), src


def test_only_a_paragraph_of_source_sentences_is_covered_by_the_program():
    """Review counterexamples: 「並非 X」 contained X, and 「X。這個說法不正確。」 was judged on X's sentence alone. The
    program covers a claim only in a paragraph made of whole source sentences; everything else is audited."""
    from app.reading.contracts import SourceClaim
    from app.reading.gates import verbatim_covered
    src = "矩形面積等於長乘以寬。正方形是長寬相等的矩形。"
    c = SourceClaim(id="C1", sentences=["S1"], statement="矩形面積等於長乘以寬。", source_text="矩形面積等於長乘以寬。")
    assert verbatim_covered(c, "我們先看定義。\n\n矩形面積等於長乘以寬。\n\n接著看例子。", src)
    assert verbatim_covered(c, "矩形面積等於長乘以寬。正方形是長寬相等的矩形。", src)
    for guide in ["並非矩形面積等於長乘以寬。", "有人誤以為矩形面積等於長乘以寬。", "只有在正方形時，矩形面積等於長乘以寬。",
                  "矩形面積等於長乘以寬。這個說法不正確。", "矩形面積等於長乘以寬，這種說法不正確。",
                  "原文指出：矩形面積等於長乘以寬。", "所謂「矩形面積等於長乘以寬」其實是迷思。"]:
        assert not verbatim_covered(c, guide, src), guide


async def test_a_negated_verbatim_claim_goes_to_the_auditor():
    negated = lambda m: {"guide": "並非" + m["claims"][0]["source_text"], "key_points": [], "check": "", "bridges_used": []}
    b = Scripted({"draft": [negated]})
    out, _ = await run(b, index=0, quotes=AREA)
    assert "- C1［" in b.prompt("body_audit") and out["audit"]["program_covered"] == []


async def test_verify_keeps_the_evidence_a_bridge_needs():
    """Review counterexample: a source-based bridge citing S999, then a re-check saying "這是正確的先備知識" with no
    valid citation, was downgraded to minor and passed Gate 2."""
    def plan(basis):
        return {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"],
                                                    "content": "可微表示函數在該點有切線斜率。", "basis": basis}]}
    bad = lambda m: {**covered_audit({**m, "bridges": []}), "bridges": [
        {"id": "B1", "verdict": "ok", "correct": "yes", "support": "S999", "guide_quote": "可微表示函數在該點有切線斜率"}]}
    bare = {"checks": [{"item": 1, "verdict": "no_problem", "reason": "這是正確的先備知識"}]}
    b = Scripted({"plan_bridges": [plan("source")], "body_audit": [bad, bad], "verify": [bare, bare]})
    out, _ = await run(b, index=0)
    assert dropped(out) and "verify" in b.steps()                # never published with the unverified bridge
    assert "S1" in b.prompt("verify") and "只寫「這是正確的」不算依據" in b.prompt("verify")   # numbered source shown

    cited = {"checks": [{"item": 1, "verdict": "no_problem", "reason": "S1 說函數在開區間內可微，切線斜率即導數"}]}
    b = Scripted({"plan_bridges": [plan("source")], "body_audit": [bad], "verify": [cited]})
    out, _ = await run(b, index=0)
    assert out["status"] == "accepted" and "revise_body" not in b.steps()
    assert any(f["code"] == "bridge_checked_by_model" for f in out["findings"])



def test_every_bridge_basis_needs_its_own_checkable_support():
    """Review counterexample: derivation / prerequisite / prior_reading / user_authorized bridges accepted
    "這是正確的". Each basis now names what must be there; a bare verdict is never support."""
    from app.reading.gates import bridge_support_problem as problem
    src = "第一句。第二句。"
    refs = {"read": {"u-a:C1"}, "reader": "我想知道可微和切線的關係"}
    bare = "這是正確的"
    for basis in ("source", "derivation", "prerequisite", "prior_reading", "user_authorized"):
        assert problem(basis, bare, src, refs), basis
        assert problem(basis, "", src, refs), basis
    assert problem("source", "S1、S999", src) and not problem("source", "S2", src)
    assert problem("derivation", "S1", src)                                     # premises without the step
    assert problem("derivation", "先求導數再代入，可得切線斜率", src)            # a step without premises
    assert not problem("derivation", "由 S1 的連續與 S2 的可微，代入均值定理可得", src)
    assert problem("prerequisite", "這是標準的先備知識", src)
    assert not problem("prerequisite", "切線斜率是導數的幾何意義，微積分入門即教", src)
    assert problem("prior_reading", "前面讀過", src, refs) and problem("prior_reading", "u-z:C9", src, refs)
    assert not problem("prior_reading", "u-a:C1 已說明連續的條件", src, refs)
    assert problem("user_authorized", "讀者要求的", src, refs) and problem("user_authorized", "「解釋一下」", src, refs)
    assert not problem("user_authorized", "讀者說「想知道可微和切線的關係」", src, refs)
    assert problem("user_authorized", "「想知道可微和切線的關係」", src, {"reader": ""})   # no request, no authorization


async def test_verify_cannot_pass_a_non_source_bridge_on_a_bare_verdict():
    def plan(basis):
        return {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"],
                                                    "content": "可微表示函數在該點有切線斜率。", "basis": basis}]}
    unsure = lambda m: {**covered_audit({**m, "bridges": []}), "bridges": [
        {"id": "B1", "verdict": "ok", "correct": "unsure", "support": "", "guide_quote": "可微表示函數在該點有切線斜率"}]}
    bare = {"checks": [{"item": 1, "verdict": "no_problem", "reason": "這是正確的"}]}
    read = LearningSnapshot(read_claims=[{"unit_id": "u-0", "claim_id": "C2", "statement": "可微必連續"}],
                            goal="想知道可微和切線的關係")
    for basis in ("derivation", "prerequisite", "prior_reading", "user_authorized"):
        b = Scripted({"plan_bridges": [plan(basis)], "body_audit": [unsure, unsure], "verify": [bare, bare]})
        out, _ = await run(b, index=0, learning=read)
        assert dropped(out) and "verify" in b.steps(), basis
    cited = {"checks": [{"item": 1, "verdict": "no_problem", "reason": "u-0:C2 已讀過可微的定義"}]}
    b = Scripted({"plan_bridges": [plan("prior_reading")], "body_audit": [unsure], "verify": [cited]})
    out, _ = await run(b, index=0, learning=read)
    assert out["status"] == "accepted" and "u-0:C2 可微必連續" in b.prompt("verify")


async def test_a_session_pinned_to_the_older_verify_prompt_still_runs_verify():
    older = {**BUNDLE["prompts"], "reading_verify": BUNDLE["prompts"]["reading_verify"]
             .replace("## 原文（程式逐句編號）\n{{sentences}}", "## 原文\n{{quote_text}}")}
    assert "{{quote_text}}" in older["reading_verify"]
    plan = {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"],
                                              "content": "可微表示函數在該點有切線斜率。", "basis": "source"}]}
    unsure = lambda m: {**covered_audit({**m, "bridges": []}), "bridges": [
        {"id": "B1", "verdict": "ok", "correct": "unsure", "support": "", "guide_quote": "可微表示函數在該點有切線斜率"}]}
    b = Scripted({"plan_bridges": [plan], "body_audit": [unsure]})
    await run(b, index=0, prompts=older)
    assert "verify" in b.steps() and QUOTES[0]["text"] in b.prompt("verify") and "{{quote_text}}" not in b.prompt("verify")


# -- third review: verbatim spans, carried budget after a failure, every cited ref valid ------------------------

async def test_whitespace_inside_code_and_strings_is_compared_verbatim():
    """Review counterexample: "a  b" (length 4) restated as "a b" passed Gate 1 as the cited text itself."""
    from app.reading.gates import _plain
    pairs = [('Python 字串 "a  b" 的長度是 4', 'Python 字串 "a b" 的長度是 4'), ("執行 `x  = 1`", "執行 `x = 1`"),
             ("    if x:\n        y = 1", "    if x:\n    y = 1"), ("$a  b$", "$a b$"), ("「a  b」", "「a b」"),
             ("```\nif x:\n    y\n```", "```\nif x:\n  y\n```")]
    for a, b in pairs:
        assert _plain(a) != _plain(b), (a, b)
    assert _plain("矩形面積 等於\n長乘以寬。") == _plain("矩形面積等於長乘以寬") and _plain("    code") == "    code"
    src = 'Python 字串 "a  b" 的長度是 4。'
    q = [{"id": "q", "index": 0, "unit_id": "u-s", "label": "第 1 行", "heading": None, "text": src}]
    b = Scripted({"understand": [{"chinese_text": None, "claims": [claim(1, ["S1"], 'Python 字串 "a b" 的長度是 4。')]}]})
    await run(b, index=0, quotes=q)
    assert "source_audit" in b.steps()
    b = Scripted({"understand": [{"chinese_text": None, "claims": [claim(1, ["S1"], src)]}]})
    await run(b, index=0, quotes=q)
    assert "source_audit" not in b.steps()


async def test_a_carried_budget_counts_every_call_of_the_new_run_failures_included(tmp_path):
    """Review counterexample: 2 calls carried over, the new run made 5 (one failed on the connection, then resumed);
    max(carried budget, trace of the new op) recorded 6, not 7. Now: carried base + every call traced under the new
    run's key."""
    from app.persistence.repository import Repository as Repo
    repo = Repo(Database(tmp_path / "db.sqlite3"))
    b = Scripted({"draft": [LLMError("Ollama 連線失敗")]})
    carried = {**CallBudget_for_guide(), "calls": 2}
    deps, task, _ = make(b, repo=repo, index=0)
    state = initial_state(task, op="s1:c1:guide:u-a:1:new", budget=carried)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:
        with pytest.raises(ReadingUnavailable):
            await invoke(build_reading_graph(deps, cp), state, {"configurable": {"thread_id": "t"}})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:   # restart
        deps, _, _ = make(b, repo=repo, index=0)
        out = await invoke(build_reading_graph(deps, cp), None, {"configurable": {"thread_id": "t"}})
    traced = repo.count_calls("s1:c1:guide:u-a:1:new:")
    assert out["status"] == "accepted" and traced == len(b.calls)          # the failed draft call is traced
    assert out["artifact"]["calls"] == 2 + traced


def CallBudget_for_guide() -> dict:
    from app.llm.runtime import CallBudget
    return CallBudget.for_task("guide").to_dict()


def test_every_cited_read_claim_and_reader_quote_must_be_valid():
    """Review counterexample: with only u-a:C1 read, "u-a:C1 與 u-never-read:C999 支持此結論" passed (any overlap)."""
    from app.reading.gates import bridge_support_problem as problem
    refs = {"read": {"u-a:C1"}, "reader": "我想知道可微和切線的關係"}
    why = problem("prior_reading", "u-a:C1 與 u-never-read:C999 支持此結論", "第一句。", refs)
    assert why and "u-never-read:C999" in why and "u-a:C1" not in why
    assert not problem("prior_reading", "u-a:C1 支持此結論", "第一句。", refs)
    why = problem("user_authorized", "讀者說「想知道可微和切線的關係」也說「請多舉例子」", "第一句。", refs)
    assert why and "請多舉例子" in why
    assert not problem("user_authorized", "讀者說「想知道可微和切線的關係」", "第一句。", refs)


# -- r10 findings: a copied skeleton value, leaked labels and loops ------------------------------------

async def test_the_audit_skeleton_gives_no_support_value_that_passes_when_copied():
    """r10: the skeleton said "support": "S1" for every bridge; the 4b model copied it for 44 prerequisite bridges.
    Each basis now gets a placeholder that fails the program check unless the model fills it in."""
    from app.reading.gates import bridge_support_problem
    from app.workflows.reading_graph import SUPPORT_SKELETON
    refs = {"read": {"u-a:C1"}, "reader": "想知道可微和切線的關係"}
    assert all(bridge_support_problem(b, v, "一。二。", refs) for b, v in SUPPORT_SKELETON.items())
    plan = {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"],
                                              "content": "可微表示函數在該點有切線斜率。", "basis": "prerequisite"}]}
    b = Scripted({"plan_bridges": [plan]})
    await run(b, index=0)
    prompt = b.prompt("body_audit")
    assert '"support": "…"' in prompt and '"support": "S1"' not in prompt


def test_leaked_labels_and_loops_in_the_guide_are_program_findings():
    """r10 published a guide reading "C1［claim］…C2［claim］…" with one sentence four times."""
    from app.reading.agent import GuideDraft, validate_draft
    t = ra.ReadingTaskInput(session_id="s", unit_id="u", index=0, total=1, label="第 1 行", text="機會成本只計算價值最高的替代方案。",
                            block_ids=["b"], material_title="m", range_label="r", model_config_hash="h", prompt_bundle_hash="p")
    loop = "機會成本只計算價值最高的那一個替代方案。"
    codes = lambda g: {f.code for f in validate_draft(t, GuideDraft(guide=g, key_points=[], check=""))}
    assert "internal_label" in codes("C1［claim］" + loop + "C2［claim］" + loop)
    assert "internal_label" in codes("依 S3 的說法，" + loop) and "internal_label" in codes("［補充說明］" + loop)
    assert "repeated_text" in codes(loop * 3) and "repeated_text" not in codes(loop * 2)
    assert not codes(loop + "也就是說，其他放棄的選項不必加總。") & {"internal_label", "repeated_text"}
    t2 = t.model_copy(update={"text": "TCP 連線狀態 S1 轉移到 S2。"})
    assert "internal_label" not in {f.code for f in validate_draft(t2, GuideDraft(guide="連線從 S1 轉移到 S2，這是三向交握的一部分。", key_points=[], check=""))}


async def test_a_guide_held_only_for_its_bridges_falls_back_to_the_source_once():
    """Bridges are optional teaching. When Gate 2 would hold a guide only because a bridge cannot be verified, the
    guide is written again from the verified source alone (once, same budget); other problems still hold it."""
    plan = {"learning_goal": "x", "bridges": [{"id": "B1", "obstacle": "不熟悉可微", "targets": ["C1"],
                                              "content": "可微表示函數在該點有切線斜率。", "basis": "prerequisite"}]}
    unsure = lambda m: {**covered_audit(m), "bridges": [{"id": b["id"], "verdict": "ok", "correct": "unsure", "support": "",
                                                         "guide_quote": b["content"]} for b in m.get("bridges") or []]}
    b = Scripted({"plan_bridges": [plan], "body_audit": [unsure, unsure]})
    out, _ = await run(b, index=0)
    steps = b.steps()
    assert dropped(out) and steps.count("draft") == 2 and steps.count("plan_bridges") == 1
    assert out["budget"]["calls"] <= out["budget"]["max_calls"]

    missing = lambda m: {**unsure(m), "claims": [{"id": c["id"], "verdict": "missing"} for c in m["claims"]]}
    b = Scripted({"plan_bridges": [plan], "body_audit": [missing] * 4})
    out, _ = await run(b, index=0)
    assert out["status"] == "failed" and b.steps().count("draft") == 1      # a missing claim is not a bridge problem

    always = lambda m: {**covered_audit(m), "bridges": [], "additions": [],
                        "claims": [{"id": c["id"], "verdict": "missing"} for c in m["claims"]]}
    supplement = lambda m: {**verbatim_body(m), "guide": verbatim_body(m)["guide"] + "\n\n補充說明：這是額外的背景。"}
    b = Scripted({"draft": [supplement, supplement], "revise_body": [supplement], "body_audit": [always] * 4})
    out, _ = await run(b, index=0)
    assert out["status"] == "failed" and b.steps().count("draft") <= 2       # falls back at most once


def test_a_bridge_about_users_or_revisions_as_subject_matter_is_not_a_production_note():
    """qwen3-vl:4b bridges on NLU (「判讀使用者意圖」) were refused four times as 產製或修訂過程 because the check
    matched the bare word 使用者; only phrases that narrate making or revising the handout are refused."""
    from app.llm.tasks import OutputInvalid
    from app.reading.contracts import BridgePlan
    from app.reading.gates import bridge_check

    def plan(content):
        return BridgePlan.model_validate({"learning_goal": "x", "bridges": [{
            "id": "B1", "obstacle": "不熟悉 NLU 與 NLG 的分工", "targets": ["C1"], "content": content, "basis": "prerequisite",
            "boundary": "integrated", "rationale": "原文只列出名稱，沒有說明分工"}]})

    check = bridge_check({"C1"}, False)
    for ok in ["NLU 專注於解析語意，例如判讀使用者意圖或情緒。", "這次比賽的結果顯示模型仍有偏誤。", "法規修訂通常需要經過公告程序。"]:
        check(plan(ok))
    for bad in ["本次修訂補上了 NLU 的定義。", "依照你的要求，這裡說明 NLG。", "根據使用者的要求補充例子。", "為了回應審稿意見，加入此段。"]:
        with pytest.raises(OutputInvalid, match="產製或修訂"):
            check(plan(bad))


async def test_an_english_source_is_written_from_its_audited_chinese_text():
    """Shown the English source, qwen3-vl:4b pasted it into the guide, revisions included: drafting and revising
    get the audited 中文課文 in its place (formulas kept); the audit still compares with the English."""
    en = [{"id": "quote-01", "index": 0, "unit_id": "u-en", "label": "p. 1", "heading": None,
           "text": "MDPs are a classical formalization of sequential decision making. The value $q_*(a)$ is estimated for each action."}]
    out, sink = await run(MockBackend(), index=0, quotes=en)
    assert out["status"] == "accepted"
    draft = next(r for r in sink.records if r.node == "draft")
    prompt = draft.request["messages"][0]["content"]
    source_section = prompt.split("## 這一段的原文")[1].split("\n## ")[0]
    assert "逐句核對過的中文課文" in source_section and "$q_*(a)$" in source_section
    assert "MDPs are a classical formalization of sequential decision making" not in source_section
    audit = next(r for r in sink.records if r.node == "body_audit")
    assert "MDPs are a classical formalization of sequential decision making" in audit.request["messages"][0]["content"]


def test_a_raised_number_is_a_footnote_not_a_missing_superscript():
    from app.reading.contracts import SymbolTranscription
    from app.reading.gates import transcription_against_clues
    tr = SymbolTranscription(formulas=["q_*(s, a)"], symbols=[])
    assert transcription_against_clues(tr, {"superscripts": ["1"]}) == []            # "…processes¹"
    assert transcription_against_clues(tr, {"superscripts": ["T"]})                    # a transpose must be read
