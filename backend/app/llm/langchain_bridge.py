"""LangChain chat model on top of the existing LLMBackend adapters.

The adapters stay the only code that talks to a model, so every verified provider behaviour is kept as is:
Ollama native /api/chat with think:false and a JSON-schema `format`; OpenVINO's own chat template with
enable_thinking=False and StructuredOutputConfig constrained decoding; the mock's task simulation through `meta`.
The bridge only converts messages and carries the per-call options:

    model = BackendChatModel(backend=..., temperature=..., max_tokens=..., context_tokens=...)
    await model.bind(json_schema=Schema.model_json_schema(), meta={"task": "guide", ...}).ainvoke(messages)

It never retries (no hidden retry layer: CallBudget in runtime.py owns every extra attempt), never declares
native tool calling (bind_tools is refused), and maps adapter errors to LLMError unchanged."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from .base import LLMBackend

# Course text must not leave the machine through LangSmith tracing; opt in explicitly with MENTOR_ALLOW_LANGSMITH=1.
if os.environ.get("MENTOR_ALLOW_LANGSMITH") != "1":
    for _k in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        os.environ[_k] = "false"

ROLE = {"human": "user", "system": "system", "ai": "assistant"}


def to_backend_messages(messages: list[BaseMessage]) -> list[dict]:
    """Text messages; an image part ({"type": "image", "base64": …}) becomes the message's `images` list (the
    Ollama chat format). Only a backend whose Capabilities declare vision is ever sent one."""
    out = []
    for m in messages:
        role = ROLE.get(m.type)
        if role is None:
            raise ValueError(f"unsupported message type for local backends: {m.type}")
        if isinstance(m.content, str):
            out.append({"role": role, "content": m.content})
            continue
        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in m.content
                       if not (isinstance(p, dict) and p.get("type") == "image"))
        images = [p["base64"] for p in m.content if isinstance(p, dict) and p.get("type") == "image"]
        out.append({"role": role, "content": text, **({"images": images} if images else {})})
    return out


class BackendChatModel(BaseChatModel):
    """One adapter + one immutable set of generation parameters (a session's model snapshot)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    backend: LLMBackend
    temperature: float = 0.2
    max_tokens: int = 512
    context_tokens: int = 8192
    semaphore: Any = None   # asyncio.Semaphore shared by everything using the same physical model

    @property
    def _llm_type(self) -> str:
        return f"mentor-{self.backend.name}"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"backend": self.backend.name, "model": self.backend.model, "temperature": self.temperature,
                "max_tokens": self.max_tokens, "context_tokens": self.context_tokens}

    def bind_tools(self, *args, **kwargs):  # noqa: D102 — capability is not declared for local adapters
        raise NotImplementedError("local adapters do not declare native tool calling; use schema-constrained "
                                  "context selection (app.llm.tasks.select_context)")

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        return asyncio.run(self._agenerate(messages, stop=stop, **kwargs))

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        meta = dict(kwargs.get("meta") or {})
        if kwargs.get("json_schema") is not None:
            meta["json_schema"] = kwargs["json_schema"]
        temperature = kwargs.get("temperature", self.temperature)
        max_tokens = max(int(kwargs.get("max_tokens") or 0), self.max_tokens)
        context_tokens = max(int(kwargs.get("context_tokens") or 0), self.context_tokens)
        payload = to_backend_messages(messages)

        async def call():
            return await self.backend.generate(payload, temperature=temperature, max_tokens=max_tokens,
                                               context_tokens=context_tokens, meta=meta)

        if self.semaphore is not None:
            async with self.semaphore:
                r = await call()
        else:
            r = await call()
        usage = r.usage or {}
        pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
        msg = AIMessage(content=r.text, response_metadata={
            "backend": self.backend.name, "model": self.backend.model, "latency_ms": r.latency_ms, "usage": usage,
            "request": {"temperature": temperature, "max_tokens": max_tokens, "context_tokens": context_tokens},
        }, usage_metadata={"input_tokens": pt or 0, "output_tokens": ct or 0, "total_tokens": (pt or 0) + (ct or 0)}
            if pt is not None or ct is not None else None)
        return ChatResult(generations=[ChatGeneration(message=msg)], llm_output={"raw": r.raw})
