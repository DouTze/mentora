"""Ollama backend (development on any machine). Uses /api/chat with JSON mode."""
from __future__ import annotations

import time
from typing import Any

import httpx

from .base import Capabilities, LLMBackend, LLMError, LLMResult


# Ollama 0.35 (qwen3-vl 2b/4b/8b) ignores think:false: the reasoning lands in `thinking`, content stays empty and the
# JSON-schema `format` is not applied. Ending the conversation with an assistant turn whose thinking block is
# already closed (what Qwen3's own template emits for enable_thinking=False, as the OpenVINO adapter does) makes
# Ollama continue that turn: content is the schema-constrained JSON again. Only for Qwen3-family models.
THINK_PREFILL = "<think>\n\n</think>\n\n"


def needs_think_prefill(model: str) -> bool:
    return model.lower().startswith("qwen3")


class OllamaBackend(LLMBackend):
    name = "ollama"

    def __init__(self, host: str, model: str, timeout: float = 180.0):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._caps: Capabilities | None = None

    async def capabilities(self) -> Capabilities:
        """From /api/show: the model's trained context length and its declared capabilities (e.g. "vision").
        num_ctx is honoured per call. If the server cannot be asked, nothing is assumed (limit unknown, no vision)."""
        if self._caps is None:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r = await client.post(f"{self.host}/api/show", json={"model": self.model})
                    r.raise_for_status()
                    d = r.json()
            except httpx.HTTPError:
                return Capabilities(context_limit=None, honors_context_tokens=True, vision=False, source="ollama:unreachable")
            ctx = next((v for k, v in (d.get("model_info") or {}).items() if k.endswith(".context_length")), None)
            self._caps = Capabilities(context_limit=int(ctx) if ctx else None, honors_context_tokens=True,
                                      vision="vision" in (d.get("capabilities") or []), source="ollama:/api/show")
        return self._caps

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None) -> LLMResult:
        if needs_think_prefill(self.model) and not (messages and messages[-1].get("role") == "assistant"):
            messages = [*messages, {"role": "assistant", "content": THINK_PREFILL}]
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # A JSON schema constrains decoding to the expected shape. Plain "json" mode let qwen3-vl:4b fall into
            # whitespace loops about half the time (Ollama: 500 "prediction aborted, token repeat limit reached").
            "format": (meta or {}).get("json_schema") or "json",
            "think": False,  # Qwen3: disable thinking so the reply is the JSON only
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": context_tokens},
        }
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.host}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama 連線失敗：{e}") from e
        latency = int((time.perf_counter() - t0) * 1000)
        usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
            "total_duration_ms": (data.get("total_duration") or 0) // 1_000_000,
            "eval_duration_ms": (data.get("eval_duration") or 0) // 1_000_000,
        }
        message = data.get("message") or {}
        # With format=json some Ollama versions put the answer in `thinking` and leave content empty.
        text = message.get("content") or message.get("thinking") or ""
        return LLMResult(text=text, latency_ms=latency, usage=usage, raw=data)

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                v = await client.get(f"{self.host}/api/version")
                v.raise_for_status()
                tags = await client.get(f"{self.host}/api/tags")
                tags.raise_for_status()
                names = [m["name"] for m in tags.json().get("models", [])]
        except httpx.HTTPError as e:
            return {"ok": False, "detail": f"無法連線 {self.host}：{e}"}
        has_model = any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
        return {
            "ok": has_model,
            "version": v.json().get("version"),
            "available_models": names,
            "detail": "就緒" if has_model else f"模型 {self.model} 未下載，請執行 ollama pull {self.model}",
        }
