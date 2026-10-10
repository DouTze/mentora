"""Guided-reading subgraph v3: a teacher's handout for one 區塊, reviewed for the points its summary names and for
conflicts with the textbook, corrected in place (never redrafted), at most three corrections; a contradiction left
is never published, anything else left is published with notes. Resume at the failed node, replies reused."""
import json

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.db import Database
from app.llm.base import Capabilities, LLMBackend, LLMError, LLMResult
from app.llm.runtime import BUDGETS, BackendPool, ListSink, ModelRuntime, ModelSnapshot
from app.persistence.repository import DbSink, Repository
from app.reading import agent as ra
from app.reading.prompts import load_bundle
from app.tutor.reading import output_language, source_language, with_language
from app.workflows import invoke
from app.workflows.reading_graph import MAX_FIXES, ReadingDeps, ReadingUnavailable, build_reading_graph, initial_state

BUNDLE = load_bundle()
SNAP = ModelSnapshot("mock", "", "", "", "", 0.2, 512, 8192)
SOURCE = ("自然語言處理是AI 領域中最具挑戰性之一，旨在使機器能夠理解、解釋並生成人類語言。"
          "NLP 技術的發展已經從簡單的語言分析進階到複雜的語意理解。")
TOPIC = {"unit_id": "u", "index": 0, "summary": "AI 的子領域",
         "chunks": [{"topic": "NLP", "aspect": "定義與發展", "summary": "NLP 讓機器理解語言，並從語言分析進階到語意理解"}]}
HANDOUT = ("這一段要回答：電腦怎麼讀懂人話？NLP 的目標是讓機器理解、解釋並生成人類語言。\n\n"
           "它的發展是從語言分析一路走到語意理解，也就是不只看字面，還要掌握意思。\n\n"
           "例如智慧客服要聽懂「我想退貨」背後的需求，這就是語意理解。")
WRONG = "它的發展是從語意理解退回到語言分析。"


def handout(text: str = HANDOUT) -> dict:
    return {"guide": text, "key_points": ["NLP 讓機器理解語言"], "check": "NLP 的發展方向是什麼？"}


def taught(m: dict) -> dict:
    return {"points": [{"id": i, "taught": "yes", "guide_quote": "讓機器理解、解釋並生成人類語言"} for i, _ in m["points"]]}


DEFAULTS = {
    "intent": lambda m: {"role": "說明 NLP 是什麼、怎麼發展", "teaches": [p for _, p in m["points"]] or ["NLP 的目標與發展"]},
    "draft": lambda m: handout(),
    "review_points": taught,
    "review_conflicts": lambda m: {"conflicts": []},
    "fix": lambda m: {"edits": []},
}


class Scripted(LLMBackend):
    """Outputs per meta['step']: a scripted list is consumed first, then the default. Records each call's meta."""
    name, model = "scripted", "scripted"

    def __init__(self, script: dict | None = None, vision: bool = False):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[dict] = []
        self.vision = vision

    async def capabilities(self):
        return Capabilities(vision=self.vision, source="test")

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
        step = (meta or {}).get("step")
        self.calls.append({"step": step, "meta": meta, "prompt": messages[0]["content"]})
        q = self.script.get(step)
        out = q.pop(0) if q else DEFAULTS[step](meta)
        if isinstance(out, Exception):
            raise out
        if callable(out):
            out = out(meta)
        return LLMResult(text=out if isinstance(out, str) else json.dumps(out, ensure_ascii=False), latency_ms=1)

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def prompt(self, step: str, n: int = 0) -> str:
        return [c["prompt"] for c in self.calls if c["step"] == step][n]


def make(backend, *, repo=None, topic=TOPIC, text=SOURCE, translate_zh=True):
    """translate_zh: the material's setting; otherwise the session writes the source's own language (read from it)."""
    out = output_language("zh-TW" if translate_zh else "source", source_language(text))
    pool = BackendPool(build=lambda c: backend)
    sink = DbSink(repo) if repo else ListSink()
    deps = ReadingDeps(runtime_for=lambda sid: ModelRuntime(SNAP, pool),
                       prompts_for=lambda sid: with_language(BUNDLE["prompts"], out), sink=sink, repo=repo,
                       render_region=lambda path, page, bbox: b"\x89PNG-stub")
    task = ra.ReadingTaskInput(session_id="s1", unit_id="u-b1", index=0, total=1, label="第 1 行", text=text,
                               block_ids=["b0"], material_title="AI 應用", range_label="第 1–2 行",
                               model_config_hash="h", prompt_bundle_hash=BUNDLE["hash"], topic=topic, language=out["code"])
    return deps, task


async def run(backend, *, repo=None, checkpointer=None, thread="t", **kw):
    deps, task = make(backend, repo=repo, **kw)
    return await invoke(build_reading_graph(deps, checkpointer), initial_state(task, op="s1:c1:guide:u-b1:1"),
                        {"configurable": {"thread_id": thread}})


def conflict_on(passage: str) -> dict:
    return {"conflicts": [{"guide_quote": passage, "source_quote": "從簡單的語言分析進階到複雜的語意理解", "why": "方向弄反",
                           "both_true": "no"}]}


async def test_a_handout_that_teaches_and_does_not_contradict_is_published_as_written():
    b = Scripted()
    out = await run(b)
    a = out["artifact"]
    assert out["status"] == "accepted" and a["guide"] == HANDOUT and a["review_status"] == "passed" and not a["revised"]
    assert b.steps() == ["intent", "draft", "review_points", "review_conflicts"]
    assert a["points"] == ["（NLP｜定義與發展）NLP 讓機器理解語言，並從語言分析進階到語意理解"]
    assert a["role"] == "說明 NLP 是什麼、怎麼發展"
    # the overview places the 導讀段 when what it teaches is read; the handout is written from the text and that only
    assert "AI 的子領域" in b.prompt("intent") and "NLP 讓機器理解語言" in b.prompt("intent")
    draft = b.prompt("draft")
    assert "這一段課文想教的內容" in draft and "說明 NLP 是什麼、怎麼發展" in draft and "NLP 讓機器理解語言" in draft
    assert "AI 的子領域" not in draft and "一律使用繁體中文" in draft


async def test_a_contradiction_is_corrected_in_place_and_only_the_changed_passage_is_reviewed_again():
    wrong = HANDOUT.replace("它的發展是從語言分析一路走到語意理解，也就是不只看字面，還要掌握意思。", WRONG)
    fixed = "它的發展是從語言分析進階到語意理解。"
    b = Scripted({"draft": [handout(wrong)], "review_conflicts": [conflict_on("從語意理解退回到語言分析")],
                  "fix": [{"edits": [{"problem": "F1", "action": "replace", "old": WRONG, "new": fixed}]}]})
    out = await run(b)
    a = out["artifact"]
    assert out["status"] == "accepted" and a["fixes"] == 1 and a["revised"]
    assert a["guide"] == wrong.replace(WRONG, fixed)                     # the other paragraphs are untouched
    assert b.steps() == ["intent", "draft", "review_points", "review_conflicts", "fix", "review_conflicts"]
    assert b.calls[-1]["meta"]["guide"] == fixed                         # only what the correction wrote
    assert "F1［和課本矛盾］" in b.prompt("fix") and "從簡單的語言分析進階到複雜的語意理解" in b.prompt("fix")


async def test_a_contradiction_left_after_three_corrections_is_not_published():
    edits = [{"edits": [{"problem": "F1", "action": "replace", "old": p, "new": f"錯誤說法第 {k} 版。"}]}
             for k, p in enumerate([WRONG, "錯誤說法第 0 版。", "錯誤說法第 1 版。"])]
    b = Scripted({"draft": [handout(HANDOUT + "\n\n" + WRONG)],
                  "review_conflicts": [conflict_on("從語意理解退回到語言分析")] + [lambda m: conflict_on(m["guide"])] * 3,
                  "fix": edits})
    out = await run(b)
    assert out["status"] == "failed" and out["error"]["code"] == "conflict_with_textbook" and out["artifact"] is None
    assert out["fixes"] == MAX_FIXES and b.steps().count("draft") == 1      # corrected, never redrafted
    assert out["guide"].endswith("錯誤說法第 2 版。")                       # the corrected handout is kept for review
    assert out["budget"]["calls"] <= BUDGETS["guide"]["max_calls"]


async def test_a_point_still_missing_after_the_corrections_is_published_with_a_note():
    no = {"points": [{"id": "P1", "taught": "no", "guide_quote": ""}]}
    b = Scripted({"review_points": [no] * (MAX_FIXES + 1),
                  "fix": [lambda m, k=k: {"edits": [{"problem": "F1", "action": "insert_after", "old": "", "new": f"補充第 {k} 次。"}]}
                          for k in range(MAX_FIXES)]})
    out = await run(b)
    a = out["artifact"]
    assert out["status"] == "accepted" and a["fixes"] == MAX_FIXES and a["review_status"] == "passed_with_notes"
    assert [u["code"] for u in a["unverified"]] == ["point_missing"] and "可能沒有教到" in a["unverified"][0]["text"]
    # each re-check asks only the point still missing, against the whole handout; conflicts only on what was added
    assert [c["meta"]["guide"] for c in b.calls if c["step"] == "review_conflicts"][1:] == ["補充第 0 次。", "補充第 1 次。", "補充第 2 次。"]


async def test_a_passage_written_back_as_it_was_is_reviewed_again_and_the_new_verdict_counts():
    """SBRL 2026-10-08: review B called an addition a contradiction; the corrector wrote the sentence back unchanged
    (it disagreed), the re-review found nothing, yet the old verdict was carried because the sentence was still in
    the handout, and the handout was held. A passage the correction wrote is judged by its re-review alone."""
    sentence = "它的發展是從語言分析一路走到語意理解，也就是不只看字面，還要掌握意思。"
    b = Scripted({"review_conflicts": [conflict_on("從語言分析一路走到語意理解"), {"conflicts": []}],
                  "fix": [{"edits": [{"problem": "F1", "action": "replace", "old": sentence, "new": " " + sentence}]}]})
    out = await run(b)
    assert out["status"] == "accepted" and out["artifact"]["fixes"] == 1 and out["artifact"]["guide"] == HANDOUT
    assert b.steps() == ["intent", "draft", "review_points", "review_conflicts", "fix", "review_conflicts"]


async def test_a_correction_that_cannot_be_applied_is_asked_again_and_stops_when_the_corrections_are_spent():
    """An edit whose passage is not in the handout changes nothing: the next correction is told which one was not
    found (no review in between: it would see the same handout); after MAX_FIXES the handout settles."""
    no = {"points": [{"id": "P1", "taught": "no", "guide_quote": ""}]}
    miss = {"edits": [{"action": "replace", "old": "講義裡沒有的句子", "new": "x"}]}
    b = Scripted({"review_points": [no, no], "fix": [miss, miss, miss]})
    out = await run(b)
    assert out["status"] == "accepted" and out["artifact"]["fixes"] == 3 and b.steps().count("fix") == 3
    assert b.steps().count("review_points") == 1
    fixes = [c["prompt"] for c in b.calls if c["step"] == "fix"]
    assert "上一次的修正沒有套用" not in fixes[0] and "上一次的修正沒有套用" in fixes[1]
    assert out["fix_log"][0]["skipped"] == ["?：要換掉的句子不在講義中"]


async def test_a_passage_copied_with_a_slip_is_still_found():
    r"""A model copying a long passage rewrites a symbol on the way (\max → \text{max}): the edit still lands."""
    from app.reading.gates import apply_edits
    from app.reading.contracts import Edit
    guide = (r"Value functions give expected returns. For instance, the optimal value satisfies $v(s) = \max_{a} q(s, a)$, "
             "which links states and actions.\n\nThe next paragraph stays.")
    old = r"For instance, the optimal value satisfies $v(s) = \text{max}_{a} q(s, a)$, which links states and actions."
    new, applied, skipped = apply_edits(guide, [Edit(action="replace", old=old, new="Rewritten.")])
    assert applied and not skipped and new == "Value functions give expected returns. Rewritten.\n\nThe next paragraph stays."
    _, applied, _ = apply_edits(guide, [Edit(action="replace", old="Something the handout never says at all, anywhere.", new="x")])
    assert not applied


async def test_a_conflict_the_reviewer_cannot_quote_is_a_note_not_a_correction():
    b = Scripted({"review_conflicts": [{"conflicts": [{"guide_quote": "NLP 只會找關鍵字", "source_quote": "課本沒有", "why": "?",
                                                       "both_true": "no"}]}]})
    out = await run(b)
    a = out["artifact"]
    assert out["status"] == "accepted" and "fix" not in b.steps() and a["guide"] == HANDOUT
    assert [u["code"] for u in a["unverified"]] == ["conflict_unconfirmed"]


async def test_a_handout_reusing_the_textbook_and_a_rewording_the_reviewer_can_reconcile_are_published_as_is():
    """The textbook's own sentences may stay where they are clear, and a passage the reviewer itself says can be
    true together with the textbook is not a contradiction: nothing is corrected."""
    reworded = {"conflicts": [{"guide_quote": "讓機器理解、解釋並生成人類語言", "why": "用字不同", "both_true": "yes",
                               "source_quote": "旨在使機器能夠理解、解釋並生成人類語言"}]}
    b = Scripted({"draft": [handout(SOURCE + "\n\n" + HANDOUT)], "review_conflicts": [reworded]})
    out = await run(b)
    assert out["status"] == "accepted" and out["artifact"]["fixes"] == 0 and "fix" not in b.steps()
    assert out["artifact"]["guide"].startswith(SOURCE) and out["artifact"]["unverified"] == []


async def test_a_handout_in_the_source_language_is_asked_for_in_it_by_name():
    """In source mode the program names the language it reads from the source: among Chinese instructions and
    Chinese summaries, "write in the source's language" got Chinese from qwen3-vl:4b; "use 英文" got English."""
    b = Scripted()
    await run(b, translate_zh=False, text="MDPs are a classical formalization of sequential decision making, where "
                                         "actions influence not just immediate rewards but also subsequent states.")
    draft = b.prompt("draft")
    assert "講義用**英文**撰寫" in draft and "一律使用英文撰寫" in draft and "一律使用繁體中文" not in draft
    assert "一律使用英文撰寫" in b.prompt("intent")                    # what it teaches is written in English too
    b = Scripted()
    await run(b, translate_zh=False)                                  # a Chinese source in source mode: Chinese
    assert "講義用**繁體中文**撰寫" in b.prompt("draft")


async def test_a_unit_without_a_summary_is_taught_from_what_its_text_teaches():
    """No summary and no overview (a rule-planned unit): what it teaches is read from its text alone, and that is
    what review A checks."""
    b = Scripted()
    out = await run(b, topic=None)
    assert out["status"] == "accepted" and out["artifact"]["points"] == ["NLP 的目標與發展"]
    assert "沒有總覽" in b.prompt("intent") and "review_points" in b.steps()


async def test_an_unreadable_intent_leaves_the_summary_to_stand_in():
    b = Scripted({"intent": ["不是 JSON"] * 3})
    out = await run(b)
    assert out["status"] == "accepted" and out["artifact"]["points"] == [
        "（NLP｜定義與發展）NLP 讓機器理解語言，並從語言分析進階到語意理解"]


async def test_symbols_that_cannot_be_confirmed_do_not_stop_the_handout():
    b = Scripted()
    deps, task = make(b, text="權重更新為 w ← w − η∇L。")
    task = task.model_copy(update={"source_kind": "pdf", "symbol_evidence": {
        "status": "needs_visual", "regions": [{"page": 1, "bbox": [0, 0, 10, 10]}], "pdf_path": "x.pdf", "clues": {},
        "reason": "PDF 文字層無法確認符號的字型"}})
    out = await invoke(build_reading_graph(deps), initial_state(task, op="s1:c1:guide:u-b1:1"),
                       {"configurable": {"thread_id": "sym"}})
    assert out["status"] == "accepted" and "symbols" not in b.steps()      # a text-only model cannot look
    assert [u["code"] for u in out["artifact"]["unverified"]] == ["notation_unverified"]


async def test_resume_after_the_model_went_away_reuses_finished_steps(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite3"))
    b = Scripted({"review_conflicts": [LLMError("Ollama 連線失敗")]})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:
        with pytest.raises(ReadingUnavailable):
            await run(b, repo=repo, checkpointer=cp)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as cp:   # a fresh saver = restart
        deps, _ = make(b, repo=repo)
        out = await invoke(build_reading_graph(deps, cp), None, {"configurable": {"thread_id": "t"}})
    assert out["status"] == "accepted"
    assert b.steps().count("draft") == 1                                   # the handout was not written again
    assert out["budget"]["calls"] >= 4                                     # the failed call still counts
