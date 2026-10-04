"""Ollama backend (development on any machine). Uses /api/chat with JSON mode.

Remote inference (opt-in): the host may be the same model served by a remote Ollama behind an authenticating proxy
(the Colab notebook in scripts/colab/), to check prompt / workflow quality faster; latency is only measured locally.
Only the model call leaves the machine; everything else stays local. Because that sends course text out, a host on
the internet (public IP or public name) is refused unless MENTOR_ALLOW_REMOTE_MODEL=1 is set. The proxy token comes
from MENTOR_OLLAMA_TOKEN (environment only, never stored in settings or session records). MENTOR_OLLAMA_SEED fixes
the sampling seed for prompt A/B runs."""
from __future__ import annotations

import ipaddress
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import Capabilities, LLMBackend, LLMError, LLMResult


# Ollama 0.35 (qwen3-vl 2b/4b/8b) ignores think:false: the reasoning lands in `thinking`, content stays empty and the
# JSON-schema `format` is not applied. Ending the conversation with an assistant turn whose thinking block is
# already closed (what Qwen3's own template emits for enable_thinking=False, as the OpenVINO adapter does) makes
# Ollama continue that turn: content is the schema-constrained JSON again. Only for Qwen3-family models.
THINK_PREFILL = "<think>\n\n</think>\n\n"


def needs_think_prefill(model: str) -> bool:
    return model.lower().startswith("qwen3")


def is_remote(host: str) -> bool:
    """True when the host is reachable only over the internet: a public IP or a dotted public name (e.g. a tunnel).
    Loopback, private / link-local addresses, single-label names (a Docker service such as `ollama`) and .local /
    .lan / .internal names stay on networks the user controls and are not treated as remote."""
    name = (urlparse(host if "//" in host else f"http://{host}").hostname or "").lower()
    if not name or name == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return "." in name and not name.endswith((".local", ".lan", ".internal", ".localhost"))
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)


def manifest_digest(model: str, models_dir: str | None = None) -> str | None:
    """The digest /api/tags reports is the sha256 of the model's manifest file, so the local model can be identified
    without a running server (compared with a remote run's digest)."""
    import hashlib
    from pathlib import Path
    name, _, tag = model.partition(":")
    ns = name if "/" in name else f"library/{name}"
    root = Path(models_dir or os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    path = root / "manifests" / "registry.ollama.ai" / ns / (tag or "latest")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def request_headers() -> dict[str, str]:
    token = os.environ.get("MENTOR_OLLAMA_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def remote_allowed() -> bool:
    return os.environ.get("MENTOR_ALLOW_REMOTE_MODEL") == "1"


def sampling_seed() -> int | None:
    v = os.environ.get("MENTOR_OLLAMA_SEED", "").strip()
    return int(v) if v.lstrip("-").isdigit() else None


class OllamaBackend(LLMBackend):
    name = "ollama"

    def __init__(self, host: str, model: str, timeout: float = 180.0):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._caps: Capabilities | None = None

    @property
    def remote(self) -> bool:
        return is_remote(self.host)

    def _guard(self) -> None:
        if self.remote and not remote_allowed():
            raise LLMError(f"模型主機 {self.host} 在網際網路上；送出前必須明確設定 MENTOR_ALLOW_REMOTE_MODEL=1"
                           "（遠端推論只供開發，會把教材內容送到該主機）")

    async def model_digest(self) -> str | None:
        """The served model's digest (from /api/tags), so a remote run can be matched to the local model."""
        try:
            self._guard()
            async with httpx.AsyncClient(timeout=10.0, headers=request_headers()) as client:
                r = await client.get(f"{self.host}/api/tags")
                r.raise_for_status()
        except (httpx.HTTPError, LLMError):
            return None
        for m in r.json().get("models", []):
            if m.get("name") == self.model or m.get("model") == self.model:
                return m.get("digest")
        return None

    async def capabilities(self) -> Capabilities:
        """From /api/show: the model's trained context length and its declared capabilities (e.g. "vision").
        num_ctx is honoured per call. If the server cannot be asked, nothing is assumed (limit unknown, no vision)."""
        if self._caps is None:
            try:
                self._guard()
                async with httpx.AsyncClient(timeout=10.0, headers=request_headers()) as client:
                    r = await client.post(f"{self.host}/api/show", json={"model": self.model})
                    r.raise_for_status()
                    d = r.json()
            except (httpx.HTTPError, LLMError):
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
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": context_tokens,
                        **({"seed": sampling_seed()} if sampling_seed() is not None else {})},
        }
        t0 = time.perf_counter()
        try:
            self._guard()
            async with httpx.AsyncClient(timeout=self.timeout, headers=request_headers()) as client:
                r = await client.post(f"{self.host}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama 連線失敗：{e}") from e
        if data.get("error"):   # the remote proxy answers 200 early (tunnel keep-alive) and reports upstream errors here
            raise LLMError(f"Ollama 錯誤：{data['error']}")
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
            self._guard()
            async with httpx.AsyncClient(timeout=5.0, headers=request_headers()) as client:
                v = await client.get(f"{self.host}/api/version")
                v.raise_for_status()
                tags = await client.get(f"{self.host}/api/tags")
                tags.raise_for_status()
                names = [m["name"] for m in tags.json().get("models", [])]
        except LLMError as e:
            return {"ok": False, "detail": str(e)}
        except httpx.HTTPError as e:
            return {"ok": False, "detail": f"無法連線 {self.host}：{e}"}
        has_model = any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
        return {
            "ok": has_model,
            "version": v.json().get("version"),
            "available_models": names,
            "detail": "就緒" if has_model else f"模型 {self.model} 未下載，請執行 ollama pull {self.model}",
        }
