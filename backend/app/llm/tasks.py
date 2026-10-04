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
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError

from .base import LLMError
from .context import PacketRecord, check_fit
from .langchain_bridge import to_backend_messages
from .runtime import BudgetExceeded, CallBudget, CallRecord, ModelRuntime, TraceSink

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
# LaTeX commands whose first letter is also a JSON escape (\b \f \n \r \t \u). A model that writes "\frac" with
# one backslash inside a JSON string gets a form feed + "rac" from a strict decoder — the formula is silently
# corrupted. Any other invalid escape ("\hat", "\sum") would fail the parse instead.
LATEX_ESC = {"bar", "beta", "bf", "big", "bigg", "bigl", "bigr", "binom", "bm", "boldsymbol", "bot", "bullet", "boxed",
             "breve", "bmod", "backslash", "frac", "forall", "flat", "frown", "natural", "nabla", "ne", "neq", "neg", "ni",
             "nu", "not", "notin", "nolimits", "norm", "nmid", "nleq", "ngeq", "nexists", "newline", "nonumber", "rho",
             "rangle", "rceil", "rfloor", "right", "rightarrow", "rightleftharpoons", "rm", "rvert", "rVert", "top", "tau",
             "theta", "times", "text", "textbf", "textit", "textrm", "texttt", "textsf", "tilde", "to", "triangle", "tfrac",
             "tbinom", "therefore", "triangleq", "underline", "uparrow", "upsilon", "underbrace", "unlhd", "uplus", "upharpoonright"}
HEX = set("0123456789abcdefABCDEF")


class TaskError(Exception):
    """kind: unavailable (connection / load) | invalid_output (still invalid after the allowed fix) | budget |
    context_overflow (the request cannot fit the model's context; no call was made)."""

    def __init__(self, kind: str, detail: str, *, records: list[CallRecord] | None = None):
        super().__init__(detail)
        self.kind, self.detail, self.records = kind, detail, records or []


class OutputInvalid(Exception):
    pass


def repair_latex_escapes(text: str) -> str:
    """Inside JSON strings, keep LaTeX backslashes: a backslash before a command in LATEX_ESC, before "u" not followed
    by four hex digits, or before any character that is not a JSON escape (\\hat, \\sum, \\{, \\,) is doubled. Real
    escapes (\\n before Chinese text, \\", \\\\, \\uXXXX) are kept."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if not in_str:
            in_str = ch == '"'
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = False
            out.append(ch)
            i += 1
            continue
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        c = text[i + 1]
        if c in '"\\/':
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "u" and i + 6 <= n and set(text[i + 2:i + 6]) <= HEX:
            out.append(text[i:i + 6])
            i += 6
            continue
        j = i + 1
        while j < n and text[j].isascii() and text[j].isalpha():
            j += 1
        word = text[i + 1:j]
        if c in "bfnrt" and word not in LATEX_ESC:
            out.append(text[i:i + 2])          # a real JSON escape (newline, tab …)
            i += 2
            continue
        out.append("\\\\")                     # LaTeX: keep the backslash itself
        i += 1
    return "".join(out)


LITERAL_NEWLINE_RE = re.compile(r"\\n(?![A-Za-z])")


def _literal_newlines(obj):
    """A model that escaped its own newline ("\\\\n") leaves a backslash + n in the text; outside a LaTeX command
    (\\nabla, \\neq …) that is always meant as a line break."""
    if isinstance(obj, str):
        return LITERAL_NEWLINE_RE.sub("\n", obj)
    if isinstance(obj, list):
        return [_literal_newlines(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _literal_newlines(v) for k, v in obj.items()}
    return obj


def parse_model_json(text: str, schema: type[BaseModel]) -> BaseModel:
    cleaned = THINK_RE.sub("", text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start == -1:
        raise OutputInvalid("輸出不含 JSON 物件")
    try:
        # the first complete object; text a model appends after it (a repeat, a remark) is ignored
        obj, _ = json.JSONDecoder().raw_decode(repair_latex_escapes(cleaned[start:]))
        obj = _literal_newlines(obj)
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
                         allow_fix: bool = True, packet: PacketRecord | None = None,
                         json_schema: dict | None = None) -> tuple[Any, list[CallRecord]]:
    """Every attempt (first try and format fix) is checked against the context budget before it is sent; a request
    that cannot fit raises TaskError("context_overflow") without calling the model. A format fix that would not fit
    with the invalid output attached is sent with the original prompt and the error note only. `json_schema`
    replaces the Pydantic schema for constrained decoding (e.g. an exact list of ids every audit must report on);
    the Pydantic model still validates the result."""
    limits = await ctx.runtime.limits(min_context_tokens)
    max_tokens = ctx.runtime.max_tokens(min_max_tokens)
    records: list[CallRecord] = []
    msgs = list(messages)
    temperature = None
    while True:
        payload = to_backend_messages(msgs)
        fit = check_fit(payload, max_tokens, limits, ctx.runtime.backend.count_tokens(payload))
        if not fit["fits"] and purpose == "format_fix" and len(msgs) > len(messages) + 1:
            msgs = list(messages) + [msgs[-1]]
            payload = to_backend_messages(msgs)
            fit = check_fit(payload, max_tokens, limits, ctx.runtime.backend.count_tokens(payload))
        if not fit["fits"]:
            raise TaskError("context_overflow", f"{node}：輸入約 {fit['estimated_input']} tokens，加上輸出保留 "
                                                f"{fit['reserved_output']} 與餘量 {fit['margin']}，超過模型可用的 {fit['limit']} tokens",
                            records=records)
        try:
            if purpose == "format_fix":
                ctx.budget.charge("format_fix")
            ctx.budget.charge_call()
        except BudgetExceeded as e:
            raise TaskError("budget", str(e), records=records) from e
        model = ctx.runtime.chat_model(min_max_tokens=max_tokens)
        bind: dict[str, Any] = {"json_schema": json_schema or schema.model_json_schema(), "meta": {"task": task, **(meta or {})},
                                "context_tokens": fit["num_ctx"]}
        if temperature is not None:
            bind["temperature"] = temperature
        model.context_tokens = fit["num_ctx"]
        rec = _record(ctx, task=task, node=node, purpose=purpose, prompt_version=prompt_version, messages=msgs,
                      source_ids=source_ids, params={k: v for k, v in bind.items() if k not in ("json_schema", "meta")})
        rec.request["context_budget"] = fit
        if packet is not None:
            rec.request["packet"] = packet.model_dump()
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
            # at most one format fix per call site, and only while the task's budget has one left
            if not allow_fix or purpose == "format_fix" or not ctx.budget.can("format_fix"):
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
    source_ids: list[str] = field(default_factory=list)   # units / items the tool actually returned
    version: str = ""
    complete: bool = True
    omitted: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args, "ok": self.ok, "content": self.content, "note": self.note,
                "source_ids": self.source_ids, "version": self.version, "complete": self.complete, "omitted": self.omitted}


class ToolDispatcher:
    """Runs whitelisted read-only tools. Rejects unknown tools / invalid arguments and skips a call identical to one
    already made in this task. Results are taken as the tool returns them (whole units, declared omissions);
    nothing is cut here."""

    def __init__(self, tools: list[BaseTool]):
        self.tools = {t.name: t for t in tools}
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
                res = await tool.ainvoke(args)
            except Exception as e:  # noqa: BLE001 — a tool's refusal (out of scope …) goes back to the model as text
                out.append(ToolResult(c.tool, args, False, "", f"工具拒絕：{e}"))
                continue
            if isinstance(res, BaseModel):   # ToolPayload
                d = res.model_dump()
                out.append(ToolResult(c.tool, args, bool(d["source_ids"]), d["text"],
                                      "；".join(d["omitted"]) and "未提供：" + "；".join(d["omitted"]),
                                      d["source_ids"], d.get("version", ""), d["complete"], d["omitted"]))
            else:
                out.append(ToolResult(c.tool, args, True, str(res)))
        return out


def format_tool_results(results: list[ToolResult]) -> str:
    if not results:
        return "（沒有讀取補充資料）"
    parts = []
    for r in results:
        head = f"### {r.tool}({json.dumps(r.args, ensure_ascii=False)})"
        body = r.content if r.ok else "（" + (r.note or "沒有內容") + "）"
        tail = f"\n（部分結果；{r.note}）" if r.ok and not r.complete else ""
        parts.append(f"{head}\n{body}{tail}")
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
