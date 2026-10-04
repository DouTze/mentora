"""LangChain bridge contract: the request each adapter sends is the same as a direct LLMBackend call, provider
specifics are kept (Ollama think:false + JSON schema format; OpenVINO own chat template + constrained decoding),
the call budget owns every retry, and context selection only runs whitelisted tools."""
import asyncio
import json
import sys
import types

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.llm.base import LLMBackend, LLMError, LLMResult
from app.llm.langchain_bridge import BackendChatModel
from app.llm.mock import MockBackend
from app.llm.ollama import OllamaBackend
from app.llm.runtime import BackendPool, CallBudget, ListSink, ModelRuntime, ModelSnapshot
from app.llm.tasks import TaskContext, TaskError, ToolDispatcher, run_structured, select_context


class Out(BaseModel):
    answer: str = Field(min_length=1)


class Scripted(LLMBackend):
    """Returns the given texts in order (an Exception is raised); records every request."""
    name, model = "scripted", "scripted"

    def __init__(self, outputs):
        self.outputs, self.requests = list(outputs), []

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None):
        self.requests.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                              "context_tokens": context_tokens, "meta": meta})
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResult(text=out, latency_ms=5, usage={"prompt_tokens": 10, "completion_tokens": 3})


def ctx_for(backend, task="guide", **budget):
    snap = ModelSnapshot("mock", "", "", "", "", 0.2, 512, 8192)
    pool = BackendPool(build=lambda cfg: backend)
    return TaskContext(runtime=ModelRuntime(snap, pool), budget=CallBudget.for_task(task, **budget), sink=ListSink())


MSGS = [SystemMessage(content="sys"), HumanMessage(content="問題")]


# -- request fidelity -------------------------------------------------------------

async def test_mock_receives_meta_and_schema_through_the_bridge():
    m = BackendChatModel(backend=MockBackend(), max_tokens=100, context_tokens=200)
    ai = await m.bind(json_schema=Out.model_json_schema(), meta={"task": "ask", "quote_text": "原文。", "question": "Q"}).ainvoke(MSGS)
    assert json.loads(ai.content)["answer"].startswith("（模擬回答）")
    assert ai.response_metadata["backend"] == "mock" and ai.response_metadata["usage"] == {"simulated": True}


async def test_ollama_request_is_identical_to_a_direct_adapter_call(monkeypatch):
    sent = []

    def handler(request: httpx.Request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"message": {"content": "", "thinking": '{"answer":"from thinking"}'},
                                         "prompt_eval_count": 12, "eval_count": 4, "total_duration": 9_000_000})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    backend = OllamaBackend("http://ollama:11434", "qwen3-vl:4b")
    schema = Out.model_json_schema()
    direct = await backend.generate([{"role": "system", "content": "sys"}, {"role": "user", "content": "問題"}],
                                    temperature=0.3, max_tokens=700, context_tokens=9000,
                                    meta={"task": "ask", "json_schema": schema})
    m = BackendChatModel(backend=backend, temperature=0.3, max_tokens=700, context_tokens=9000)
    ai = await m.bind(json_schema=schema, meta={"task": "ask"}).ainvoke(MSGS)
    assert sent[0] == sent[1]
    path, payload = sent[1]
    assert path == "/api/chat" and payload["think"] is False and payload["stream"] is False
    assert payload["format"] == schema   # a JSON schema, never the plain "json" mode
    assert payload["options"] == {"temperature": 0.3, "num_predict": 700, "num_ctx": 9000}
    assert [x["role"] for x in payload["messages"]] == ["system", "user", "assistant"]
    assert payload["messages"][-1]["content"] == "<think>\n\n</think>\n\n"   # Qwen3: thinking closed up front
    # qwen3-vl puts the answer in `thinking` with empty content: the adapter's fallback still applies
    assert ai.content == direct.text == '{"answer":"from thinking"}'
    assert ai.usage_metadata["input_tokens"] == 12 and ai.response_metadata["usage"]["completion_tokens"] == 4


async def test_ollama_connection_error_is_not_retried_by_any_layer(monkeypatch):
    calls = []

    def handler(request):
        if request.url.path == "/api/chat":   # the capability probe (/api/show) is not a model call
            calls.append(1)
        raise httpx.ConnectError("refused")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    ctx = ctx_for(OllamaBackend("http://x", "m"))
    with pytest.raises(TaskError) as e:
        await run_structured(ctx, MSGS, Out, task="ask", node="answer", prompt_version="t")
    assert e.value.kind == "unavailable" and len(calls) == 1 and ctx.budget.calls == 1
    assert ctx.sink.records[0].error_kind == "unavailable"


def _fake_openvino(captured):
    mod = types.ModuleType("openvino_genai")

    class GenerationConfig:
        pass

    class StructuredOutputConfig:
        def __init__(self, json_schema):
            self.json_schema = json_schema

    class Tok:
        def apply_chat_template(self, messages, add_generation_prompt, extra_context):
            captured["template"] = {"messages": messages, "add_generation_prompt": add_generation_prompt,
                                    "extra_context": extra_context}
            return "<prompt>"

    class Perf:
        def get_num_input_tokens(self): return 21
        def get_num_generated_tokens(self): return 7
        def get_ttft(self): return types.SimpleNamespace(mean=12.0)
        def get_throughput(self): return types.SimpleNamespace(mean=33.3)

    class Pipe:
        def __init__(self, path, device, **kw):
            captured["load"] = {"path": path, "device": device, **kw}

        def get_tokenizer(self):
            return Tok()

        def generate(self, prompts, cfg):
            captured["generate"] = {"prompts": prompts, "cfg": cfg}
            return types.SimpleNamespace(texts=['{"answer":"ov"}'], perf_metrics=Perf())

    mod.GenerationConfig, mod.StructuredOutputConfig, mod.LLMPipeline = GenerationConfig, StructuredOutputConfig, Pipe
    return mod


async def test_openvino_keeps_own_chat_template_and_constrained_decoding(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "openvino_genai", _fake_openvino(captured))
    from app.llm.openvino import OpenVINOBackend
    schema = Out.model_json_schema()
    m = BackendChatModel(backend=OpenVINOBackend("/models/q", "GPU"), temperature=0.0, max_tokens=300, context_tokens=4096)
    ai = await m.bind(json_schema=schema, meta={"task": "ask"}).ainvoke(MSGS)
    cfg = captured["generate"]["cfg"]
    assert ai.content == '{"answer":"ov"}' and captured["generate"]["prompts"] == ["<prompt>"]
    assert cfg.apply_chat_template is False                       # template applied once, by the adapter
    assert json.loads(cfg.structured_output_config.json_schema) == schema
    assert cfg.max_new_tokens == 300 and cfg.do_sample is False
    assert captured["template"]["extra_context"] == {"enable_thinking": False}
    assert captured["template"]["add_generation_prompt"] is True
    assert [x["role"] for x in captured["template"]["messages"]] == ["system", "user"]
    assert captured["load"]["CACHE_DIR"].endswith(".ov_cache")
    assert ai.response_metadata["usage"]["tokens_per_sec"] == 33.3


async def test_think_prefill_only_for_qwen3_and_not_doubled(monkeypatch):
    sent = []
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda r: (sent.append(json.loads(r.content)), httpx.Response(200, json={"message": {"content": "{}"}}))[1]), **kw))
    await OllamaBackend("http://x", "llama3.2").generate([{"role": "user", "content": "q"}], temperature=0, max_tokens=1, context_tokens=1)
    await OllamaBackend("http://x", "qwen3-vl:4b").generate([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
                                                          temperature=0, max_tokens=1, context_tokens=1)
    assert [m["role"] for m in sent[0]["messages"]] == ["user"]
    assert [m["role"] for m in sent[1]["messages"]] == ["user", "assistant"]


def test_native_tool_calling_is_not_declared():
    with pytest.raises(NotImplementedError):
        BackendChatModel(backend=MockBackend()).bind_tools([])


# -- budget -------------------------------------------------------------------------

async def test_one_format_fix_with_concrete_feedback_then_success():
    b = Scripted(['{"answer": ""}', '{"answer": "好"}'])
    ctx = ctx_for(b)
    out, recs = await run_structured(ctx, MSGS, Out, task="guide", node="draft", prompt_version="v1")
    assert out.answer == "好" and ctx.budget.calls == 2 and ctx.budget.format_fixes == 1
    assert [r.purpose for r in recs] == ["first", "format_fix"]
    fix_prompt = b.requests[1]["messages"][-1]["content"]
    assert "answer" in fix_prompt and b.requests[1]["temperature"] == 0.0
    assert b.requests[1]["messages"][-2] == {"role": "assistant", "content": '{"answer": ""}'}


async def test_format_fix_is_spent_once_per_task_not_per_call():
    b = Scripted(["nope", '{"answer": "a"}', "still nope"])
    ctx = ctx_for(b, task="ask")   # one format fix for the whole task
    await run_structured(ctx, MSGS, Out, task="ask", node="ask.context", prompt_version="v1")
    with pytest.raises(TaskError) as e:
        await run_structured(ctx, MSGS, Out, task="ask", node="ask.answer", prompt_version="v1")
    assert e.value.kind == "invalid_output" and len(b.requests) == 3 and ctx.budget.calls == 3


async def test_one_call_site_never_spends_two_format_fixes():
    b = Scripted(["nope", "still nope", '{"answer": "a"}'])
    ctx = ctx_for(b, task="guide")   # the guide task has two fixes, but one node gets one
    with pytest.raises(TaskError) as e:
        await run_structured(ctx, MSGS, Out, task="guide", node="draft", prompt_version="v1")
    assert e.value.kind == "invalid_output" and len(b.requests) == 2 and ctx.budget.format_fixes == 1


async def test_call_budget_is_a_hard_cap():
    b = Scripted(['{"answer": "a"}'] * 5)
    ctx = ctx_for(b, task="assess")   # 2 calls
    await run_structured(ctx, MSGS, Out, task="assess", node="a", prompt_version="v1")
    await run_structured(ctx, MSGS, Out, task="assess", node="a", prompt_version="v1")
    with pytest.raises(TaskError) as e:
        await run_structured(ctx, MSGS, Out, task="assess", node="a", prompt_version="v1")
    assert e.value.kind == "budget" and len(b.requests) == 2


def test_budget_round_trips_as_plain_data():
    b = CallBudget.for_task("guide")
    b.charge_call()
    b.charge("revision")
    b.charge("revision")
    assert CallBudget.from_dict(json.loads(json.dumps(b.to_dict()))) == b
    assert not b.can("revision")


async def test_semaphore_serialises_inference_on_one_physical_model():
    active, peak = 0, 0

    class Slow(LLMBackend):
        name, model = "slow", "slow"

        async def generate(self, messages, **kw):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return LLMResult(text='{"answer":"x"}', latency_ms=1)

    pool = BackendPool(build=lambda cfg: Slow())
    s1 = ModelSnapshot("ollama", "m", "h", "", "", 0.2, 512, 8192)
    s2 = ModelSnapshot("ollama", "m", "h", "", "", 0.7, 999, 8192)   # same model, other params → same lock
    assert pool.semaphore(s1) is pool.semaphore(s2) and s1.config_hash != s2.config_hash
    models = [ModelRuntime(s, pool).chat_model() for s in (s1, s2, s1)]
    await asyncio.gather(*(m.ainvoke(MSGS) for m in models))
    assert peak == 1


# -- context selection ------------------------------------------------------------------

class UnitArgs(BaseModel):
    unit_ids: list[str] = Field(default_factory=list, max_length=3)


def _tools(log):
    async def read_units(unit_ids):
        log.append(unit_ids)
        if "u9" in unit_ids:
            raise ValueError("超出本次學習範圍")
        return "\n".join(f"[{u}] text" for u in unit_ids)
    return [StructuredTool.from_function(coroutine=read_units, name="read_source_units",
                                         description="讀取指定來源單元", args_schema=UnitArgs)]


async def test_context_selection_validates_dedupes_and_respects_rounds():
    log = []
    req1 = {"action": "read", "reason": "需要前文", "calls": [
        {"tool": "read_source_units", "args": {"unit_ids": ["u1"]}},
        {"tool": "drop_table", "args": {}},
    ]}
    req2 = {"action": "read", "reason": "再讀", "calls": [
        {"tool": "read_source_units", "args": {"unit_ids": ["u1"]}},      # same call again → skipped
        {"tool": "read_source_units", "args": {"unit_ids": ["u9"]}},      # tool refuses: out of scope
    ]}
    b = Scripted([json.dumps(req1), json.dumps(req2)])
    ctx = ctx_for(b)
    results, recs = await select_context(ctx, MSGS, ToolDispatcher(_tools(log)), task="guide", node="context",
                                         prompt_version="v1")
    assert log == [["u1"], ["u9"]]
    assert [(r.tool, r.ok, r.note) for r in results] == [
        ("read_source_units", True, ""), ("drop_table", False, "未知的工具"),
        ("read_source_units", False, "重複的讀取，已略過"), ("read_source_units", False, "工具拒絕：超出本次學習範圍")]
    assert ctx.budget.tool_rounds == 2 and len(b.requests) == 2      # guide allows 2 rounds, no third call
    schema = b.requests[0]["meta"]["json_schema"]
    assert schema["properties"]["calls"]["items"]["properties"]["tool"]["enum"] == ["read_source_units"]
    assert schema["properties"]["calls"]["maxItems"] == 2
    assert "已讀取的補充資料" in b.requests[1]["messages"][-1]["content"]


async def test_invalid_context_request_ends_selection_without_spending_the_format_fix():
    b = Scripted(["not json"])
    ctx = ctx_for(b)
    results, _ = await select_context(ctx, MSGS, ToolDispatcher(_tools([])), task="guide", node="context", prompt_version="v1")
    assert results == [] and ctx.budget.format_fixes == 0 and ctx.budget.calls == 1
