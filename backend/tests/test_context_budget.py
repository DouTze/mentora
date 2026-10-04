"""R2: one budget entry for every model call, packets that drop only optional context, real backend limits, and
tool results that are never cut inside a unit."""
import json

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from app.llm.base import Capabilities, LLMBackend, LLMResult
from app.llm.context import (ContextItem, ContextOverflow, Packet, check_fit, estimate_messages, estimate_tokens,
                             resolve_limits)
from app.llm.openvino import OpenVINOBackend
from app.llm.runtime import BackendPool, CallBudget, ListSink, ModelRuntime, ModelSnapshot
from app.llm.tasks import TaskContext, TaskError, ToolCallRequest, ToolDispatcher, run_structured
from app.reading.tools import ReadingScope, build_tools


class Out(BaseModel):
    answer: str


class Limited(LLMBackend):
    """A backend that declares a small context and records what it was asked."""
    name, model = "limited", "limited"

    def __init__(self, limit: int, outputs: list[str], honors: bool = True):
        self.limit, self.outputs, self.honors = limit, list(outputs), honors
        self.requests: list[dict] = []

    async def capabilities(self):
        return Capabilities(context_limit=self.limit, honors_context_tokens=self.honors, source="test")

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
        self.requests.append({"messages": messages, "max_tokens": max_tokens, "context_tokens": context_tokens})
        return LLMResult(text=self.outputs.pop(0), latency_ms=1)


def ctx_for(backend, snap=None, task="ask") -> TaskContext:
    snap = snap or ModelSnapshot("mock", "", "", "", "", 0.2, 512, 8192)
    return TaskContext(ModelRuntime(snap, BackendPool(build=lambda c: backend)), CallBudget.for_task(task), ListSink())


def test_fit_counts_input_output_and_margin():
    limits = resolve_limits(Capabilities(context_limit=8000, honors_context_tokens=True), 4096)
    msgs = [{"role": "user", "content": "中" * 3000}]
    fit = check_fit(msgs, 1024, limits)
    assert fit["estimated_input"] == estimate_messages(msgs) >= 3000
    assert fit["need"] == fit["estimated_input"] + 1024 + fit["margin"] and fit["fits"]
    assert not check_fit([{"role": "user", "content": "中" * 7000}], 1024, limits)["fits"]


def test_ollama_num_ctx_is_a_stable_bucket_never_above_the_model():
    limits = resolve_limits(Capabilities(context_limit=262144, honors_context_tokens=True), 8192)
    assert limits.limit == 32768                      # MENTOR_MAX_CONTEXT_TOKENS caps a 256k model
    assert limits.num_ctx(3000) == limits.num_ctx(9000) == 16384   # small calls share one runner size
    assert limits.num_ctx(20000) == 20480 and limits.num_ctx(40000) == 32768
    small = resolve_limits(Capabilities(context_limit=4096, honors_context_tokens=True), 8192)
    assert small.limit == 4096 and small.num_ctx(100) == 4096      # the session setting cannot exceed the model


def test_openvino_limit_comes_from_the_exported_model_not_context_tokens(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"max_position_embeddings": 40960}))
    import asyncio
    caps = asyncio.run(OpenVINOBackend(str(tmp_path), "CPU").capabilities())
    assert caps.context_limit == 40960 and not caps.honors_context_tokens and not caps.vision
    limits = resolve_limits(caps, 8192)
    assert limits.limit == 40960 and limits.num_ctx(30000) == limits.floor   # num_ctx is not a lever there


async def test_a_call_that_cannot_fit_is_not_sent():
    b = Limited(2048, ['{"answer":"x"}'])
    ctx = ctx_for(b)
    with pytest.raises(TaskError) as e:
        await run_structured(ctx, [HumanMessage(content="中" * 2500)], Out, task="ask", node="answer", prompt_version="t")
    assert e.value.kind == "context_overflow" and b.requests == [] and ctx.budget.calls == 0
    assert "超過模型可用的 2048" in e.value.detail


async def test_every_call_records_its_budget_and_uses_the_bucket():
    b = Limited(262144, ['{"answer":"x"}'])
    ctx = ctx_for(b)
    await run_structured(ctx, [HumanMessage(content="問題")], Out, task="ask", node="answer", prompt_version="t",
                         min_max_tokens=1024)
    budget = ctx.sink.records[0].request["context_budget"]
    assert budget["fits"] and budget["reserved_output"] == 1024 and budget["num_ctx"] == 16384
    assert b.requests[0]["context_tokens"] == 16384 and b.requests[0]["max_tokens"] == 1024


async def test_format_fix_that_would_overflow_drops_the_invalid_output_not_the_prompt():
    big_bad = "x" * 9000                              # ~3150 tokens of invalid output
    b = Limited(5200, [big_bad, '{"answer":"ok"}'])
    ctx = ctx_for(b)
    out, recs = await run_structured(ctx, [HumanMessage(content="中" * 3000)], Out, task="ask", node="answer",
                                     prompt_version="t")
    assert out.answer == "ok" and len(b.requests) == 2
    fix = b.requests[1]["messages"]
    assert fix[0]["content"] == "中" * 3000 and all(m["content"] != big_bad for m in fix)
    assert "不符合要求" in fix[-1]["content"]


def test_packet_drops_optional_context_lowest_priority_first_and_never_required():
    limits = resolve_limits(Capabilities(context_limit=3000, honors_context_tokens=True), 1024)
    tpl = "{{source}}\n{{qa}}\n{{prev}}"
    items = [ContextItem(key="source", text="原" * 1200, source_ids=["u1"]),
             ContextItem(key="qa", text="問" * 600, required=False, priority=0),
             ContextItem(key="prev", text="前" * 400, required=False, priority=1)]
    msg, rec = Packet("draft", tpl, items).build(512, limits)
    assert rec.dropped == ["qa"] and "原" * 1200 in msg and "前" * 400 in msg and "沒有提供" in msg
    assert rec.fit["fits"] and [i["included"] for i in rec.items] == [True, False, True]
    with pytest.raises(ContextOverflow) as e:
        Packet("draft", tpl, [ContextItem(key="source", text="原" * 2800)] + items[1:]).build(512, limits)
    assert "必要內容" in e.value.detail


async def test_tool_results_keep_whole_units_and_say_what_was_left_out():
    """A necessary condition at the end of a long unit is never cut off: the unit is either whole or declared
    omitted, and the payload names the units actually returned."""
    long_unit = "樣本" * 900 + "但只有在樣本彼此獨立時，這個結論才成立。"
    quotes = [{"id": "q1", "index": 0, "unit_id": "u-a", "label": "第 1 行", "text": long_unit},
              {"id": "q2", "index": 1, "unit_id": "u-b", "label": "第 2 行", "text": "前一段的結論可以推廣。"},
              {"id": "q3", "index": 2, "unit_id": "u-c", "label": "第 3 行", "text": "這兩個前提缺一不可。"}]
    tools = build_tools(ReadingScope(quotes=quotes, current_index=2, version="snap-1"))
    d = ToolDispatcher(tools)
    res = await d.run([ToolCallRequest(tool="read_source_units", args={"unit_ids": ["u-a", "u-b"]})], 2)
    r = res[0]
    assert r.ok and r.source_ids == ["u-b"] and not r.complete and r.version == "snap-1"
    assert "u-a" in r.omitted[0] and "樣本彼此獨立" not in r.content   # not half a unit
    res = await d.run([ToolCallRequest(tool="read_neighbor_units", args={"unit_id": "u-c", "before": 1})], 2)
    assert res[0].source_ids == ["u-b"] and res[0].complete


def test_estimate_is_conservative_for_cjk_and_latin():
    assert estimate_tokens("中文") == 2 and estimate_tokens("a" * 100) == 35


def test_single_backslash_latex_in_model_json_is_not_turned_into_control_characters():
    """\\frac, \\bar, \\top, \\nabla written with one backslash would decode as form feed / backspace / tab / newline."""
    from app.llm.tasks import parse_model_json

    class G(BaseModel):
        g: str
    raw = r'{"g": "$\bar{y}_i = \mathbf{w}^\top x$，$\frac{1}{n}\sum$\n第二段 \nabla L \\hat{y}"}'
    g = parse_model_json(raw, G).g
    assert "\\bar{y}_i" in g and "\\top" in g and "\\frac{1}{n}" in g and "\\sum" in g and "\\nabla" in g
    assert "\n第二段" in g and "\\hat{y}" in g and not any(ch in g for ch in "\b\f\t")


def test_a_self_escaped_newline_becomes_a_line_break_but_latex_commands_stay():
    from app.llm.tasks import parse_model_json

    class G(BaseModel):
        g: str
    g = parse_model_json('{"g": "第一段。\\\\n\\\\n第二段 $\\\\nabla L$"}', G, prose_fields=("g",)).g
    assert g == "第一段。\n\n第二段 $\\nabla L$"


def test_valid_json_keeps_code_regex_and_quotes_exactly():
    """Counterexample from review: print("\\n") in valid JSON must stay a backslash + n, in any field."""
    import json as _json
    from app.llm.tasks import parse_model_json

    class G(BaseModel):
        guide: str
        quote: str

    guide = '第一段。\n\n程式：\n```python\nprint("\\n")\n```\n正則 `\\d+\\n` 也一樣。'
    quote = 'print("\\n")'
    raw = _json.dumps({"guide": guide, "quote": quote}, ensure_ascii=False)
    g = parse_model_json(raw, G, prose_fields=("guide",))
    assert g.guide == guide and g.quote == quote
    one_line = _json.dumps({"guide": '用 `print("\\n")` 換行，或寫 ```\\n```', "quote": quote}, ensure_ascii=False)
    g = parse_model_json(one_line, G, prose_fields=("guide",))   # no real line break, but the \n is inside code
    assert g.guide == '用 `print("\\n")` 換行，或寫 ```\\n```'


def test_the_guide_schema_bounds_its_strings_by_the_output_budget():
    """A degenerate repetition cannot run to the token limit and leave unparsable JSON: the guide string is bounded."""
    from app.reading.gates import body_schema, body_tokens
    short, long_ = "短句。" * 10, "這是一個很長的段落。" * 200
    assert body_tokens(short) == 2048 and body_tokens(long_) == 4096
    sch = body_schema(short)
    assert sch["properties"]["guide"]["maxLength"] == int(2048 * 0.8)
    assert sch["properties"]["key_points"]["maxItems"] == 3 and sch["properties"]["limits"]["maxItems"] == 4
    assert body_schema(long_)["properties"]["guide"]["maxLength"] > sch["properties"]["guide"]["maxLength"]

