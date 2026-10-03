"""Structured model tasks on the LangChain bridge.

run_structured: messages → BackendChatModel (schema-constrained) → JSON → Pydantic → optional program check.
  An invalid output may be fixed once (feedback names the concrete validation error), only if the task's
  CallBudget still allows a format fix. Connection errors are never retried here.

select_context: schema-constrained tool selection, the common capability of the local models. The model returns
  a ContextRequest (`ready`, or up to N calls of whitelisted read-only tools); the dispatcher validates the tool
  name and arguments against the LangChain tool's args_schema, drops repeats of the same call, runs the tools and
  returns their results for the next step. Native bind_tools is not used (see langchain_bridge)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError

from .base import LLMError
from .runtime import BudgetExceeded, CallBudget, CallRecord, ModelRuntime, TraceSink

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class TaskError(Exception):
    """kind: unavailable (connection / load) | invalid_output (still invalid after the allowed fix) | budget."""

    def __init__(self, kind: str, detail: str, *, records: list[CallRecord] | None = None):
        super().__init__(detail)
        self.kind, self.detail, self.records = kind, detail, records or []


class OutputInvalid(Exception):
    pass


def parse_model_json(text: str, schema: type[BaseModel]) -> BaseModel:
    cleaned = THINK_RE.sub("", text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start == -1:
        raise OutputInvalid("輸出不含 JSON 物件")
    try:
        # the first complete object; text a model appends after it (a repeat, a remark) is ignored
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        return schema.model_validate(obj)
    except ValidationError as e:
        err = e.errors()[0]
        raise OutputInvalid(f"欄位不符：{err['msg']} @ {'.'.join(str(x) for x in err['loc'])}") from e
    except ValueError as e:
        raise OutputInvalid(f"JSON 解析失敗：{e}") from e


@dataclass
class TaskContext:
    """Who is calling: the budget all calls are charged to and where the trace goes."""
    runtime: ModelRuntime
    budget: CallBudget
    sink: TraceSink
    session_id: str | None = None
    command_id: str | None = None
    operation_key: str | None = None


def _record(ctx: TaskContext, *, task, node, purpose, prompt_version, messages, source_ids, params) -> CallRecord:
    return CallRecord(task=task, node=node, attempt=ctx.budget.calls, purpose=purpose, prompt_version=prompt_version,
                      request={"messages": [{"role": m.type, "content": m.content} for m in messages], **params},
                      source_ids=list(source_ids), backend=ctx.runtime.backend.name, model=ctx.runtime.backend.model,
                      session_id=ctx.session_id, command_id=ctx.command_id, operation_key=ctx.operation_key)


async def run_structured(ctx: TaskContext, messages: list[BaseMessage], schema: type[BaseModel], *, task: str, node: str,
                         prompt_version: str, meta: dict | None = None, source_ids: list[str] = (),
                         check: Callable[[Any], None] | None = None, min_max_tokens: int = 0,
                         min_context_tokens: int = 0, purpose: str = "first",
                         allow_fix: bool = True) -> tuple[Any, list[CallRecord]]:
    model = ctx.runtime.chat_model(min_max_tokens=min_max_tokens, min_context_tokens=min_context_tokens)
    records: list[CallRecord] = []
    msgs = list(messages)
    temperature = None
    while True:
        try:
            if purpose == "format_fix":
                ctx.budget.charge("format_fix")
            ctx.budget.charge_call()
        except BudgetExceeded as e:
            raise TaskError("budget", str(e), records=records) from e
        bind: dict[str, Any] = {"json_schema": schema.model_json_schema(), "meta": {"task": task, **(meta or {})}}
        if temperature is not None:
            bind["temperature"] = temperature
        rec = _record(ctx, task=task, node=node, purpose=purpose, prompt_version=prompt_version, messages=msgs,
                      source_ids=source_ids, params={k: v for k, v in bind.items() if k not in ("json_schema", "meta")})
        chain = model.bind(**bind) | RunnableLambda(lambda m: (m, m.content))
        try:
            ai, text = await chain.ainvoke(msgs)
        except LLMError as e:
            rec.error, rec.error_kind = str(e), "unavailable"
            ctx.sink.record(rec)
            records.append(rec)
            raise TaskError("unavailable", str(e), records=records) from e
        rec.raw, rec.latency_ms = text, ai.response_metadata.get("latency_ms")
        rec.usage = ai.response_metadata.get("usage") or {}
        try:
            parsed = parse_model_json(text, schema)
            if check:
                check(parsed)
        except OutputInvalid as e:
            rec.error, rec.error_kind = str(e), "invalid_output"
            ctx.sink.record(rec)
            records.append(rec)
            if not allow_fix or not ctx.budget.can("format_fix"):
                raise TaskError("invalid_output", str(e), records=records) from e
            msgs = msgs + [AIMessage(content=text), HumanMessage(content=(
                f"你的輸出不符合要求：{e}。請修正這個問題，只輸出一個符合格式的 JSON 物件，不要有其他文字。"))]
            purpose, temperature = "format_fix", 0.0
            continue
        rec.validated = parsed.model_dump()
        ctx.sink.record(rec)
        records.append(rec)
        return parsed, records


# -- schema-constrained context selection ---------------------------------------------

class ToolCallRequest(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ContextRequest(BaseModel):
    action: Literal["ready", "read"]
    calls: list[ToolCallRequest] = Field(default_factory=list)
    reason: str = ""


def context_request_schema(tools: list[BaseTool], max_calls: int) -> dict:
    """JSON schema with the tool name restricted to the whitelist; arguments are the union of all tools' fields
    (small models follow one flat object better than anyOf); each call is re-validated per tool afterwards."""
    props: dict[str, Any] = {}
    for t in tools:
        for k, v in (t.args_schema.model_json_schema().get("properties") or {}).items():
            props.setdefault(k, {kk: vv for kk, vv in v.items() if kk != "title"})
    return {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["ready", "read"]},
        "calls": {"type": "array", "maxItems": max_calls, "items": {"type": "object", "properties": {
            "tool": {"type": "string", "enum": [t.name for t in tools]},
            "args": {"type": "object", "properties": props}}, "required": ["tool", "args"]}},
        "reason": {"type": "string"}}, "required": ["action", "calls", "reason"]}


@dataclass
class ToolResult:
    tool: str
    args: dict
    ok: bool
    content: str
    note: str = ""


class ToolDispatcher:
    """Runs whitelisted read-only tools. Rejects unknown tools / invalid arguments, skips a call identical to one
    already made in this task, and truncates each result to `max_chars`."""

    def __init__(self, tools: list[BaseTool], *, max_chars: int = 4000):
        self.tools = {t.name: t for t in tools}
        self.max_chars = max_chars
        self.seen: set[str] = set()

    async def run(self, calls: list[ToolCallRequest], limit: int) -> list[ToolResult]:
        out: list[ToolResult] = []
        for c in calls[:limit]:
            tool = self.tools.get(c.tool)
            if tool is None:
                out.append(ToolResult(c.tool, c.args, False, "", "未知的工具"))
                continue
            try:
                args = tool.args_schema.model_validate(c.args).model_dump()
            except ValidationError as e:
                out.append(ToolResult(c.tool, c.args, False, "", f"參數不符：{e.errors()[0]['msg']}"))
                continue
            key = json.dumps([c.tool, args], sort_keys=True, ensure_ascii=False)
            if key in self.seen:
                out.append(ToolResult(c.tool, args, False, "", "重複的讀取，已略過"))
                continue
            self.seen.add(key)
            try:
                content = str(await tool.ainvoke(args))
            except Exception as e:  # noqa: BLE001 — a tool's refusal (out of scope …) goes back to the model as text
                out.append(ToolResult(c.tool, args, False, "", f"工具拒絕：{e}"))
                continue
            note = ""
            if len(content) > self.max_chars:
                content, note = content[:self.max_chars], f"已截斷至 {self.max_chars} 字"
            out.append(ToolResult(c.tool, args, True, content, note))
        return out


def format_tool_results(results: list[ToolResult]) -> str:
    if not results:
        return "（沒有讀取補充資料）"
    parts = []
    for r in results:
        head = f"### {r.tool}({json.dumps(r.args, ensure_ascii=False)})"
        parts.append(f"{head}\n{r.content if r.ok else '（' + r.note + '）'}" + (f"\n（{r.note}）" if r.ok and r.note else ""))
    return "\n\n".join(parts)


async def select_context(ctx: TaskContext, base_messages: list[BaseMessage], dispatcher: ToolDispatcher, *,
                         task: str, node: str, prompt_version: str, meta: dict | None = None,
                         source_ids: list[str] = ()) -> tuple[list[ToolResult], list[CallRecord]]:
    """Up to budget.max_tool_rounds rounds; each round is one schema-constrained call. Stops at `ready`, when a
    round adds nothing new, or when the budget has no tool round left. Invalid requests end the selection (the
    task continues with what it has) rather than spending the task's single format fix."""
    gathered: list[ToolResult] = []
    records: list[CallRecord] = []
    limit = ctx.budget.max_tools_per_round
    schema = context_request_schema(list(dispatcher.tools.values()), limit)

    class _Req(ContextRequest):
        @classmethod
        def model_json_schema(cls, *a, **k):  # the constrained-decoding schema carries the tool whitelist
            return schema

    while ctx.budget.can("tool_round"):
        ctx.budget.charge("tool_round")
        msgs = list(base_messages)
        if gathered:
            msgs.append(HumanMessage(content="## 已讀取的補充資料\n" + format_tool_results(gathered)
                                     + "\n\n若還需要其他資料，再提出讀取；否則回答 ready。"))
        try:
            req, recs = await run_structured(ctx, msgs, _Req, task=task, node=node, prompt_version=prompt_version,
                                             meta={**(meta or {}), "step": "context"}, source_ids=source_ids,
                                             purpose="context", allow_fix=False)
        except TaskError as e:
            records += e.records
            if e.kind == "invalid_output":
                break
            raise
        records += recs
        if req.action == "ready" or not req.calls:
            break
        new = await dispatcher.run(req.calls, limit)
        ctx.budget.tool_calls += len(new)
        gathered += new
        if not any(r.ok for r in new):
            break
    return gathered, records
