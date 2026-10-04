from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Raised when the backend cannot produce a response (connection, timeout, load failure)."""


@dataclass(frozen=True)
class Capabilities:
    """What the loaded model can actually take, as reported by the backend (never guessed from a model name).

    context_limit: tokens the model can attend to (input + output); None = unknown / not limited (mock).
    honors_context_tokens: the adapter applies the per-call context size (Ollama num_ctx). When False the limit is
        fixed by the loaded model and `context_tokens` has no effect (OpenVINO GenAI's LLMPipeline).
    vision: images can be sent with a message through this adapter.
    source: where the facts came from, for the trace."""
    context_limit: int | None = None
    honors_context_tokens: bool = True
    vision: bool = False
    source: str = "default"


@dataclass
class LLMResult:
    text: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


class LLMBackend:
    name: str = "base"
    model: str = ""
    simulated: bool = False

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        context_tokens: int,
        meta: dict[str, Any] | None = None,
    ) -> LLMResult:
        raise NotImplementedError

    async def health(self) -> dict[str, Any]:
        raise NotImplementedError

    async def capabilities(self) -> Capabilities:
        return Capabilities()

    def count_tokens(self, messages: list[dict[str, str]]) -> int | None:
        """Exact prompt tokens when the backend has its tokenizer loaded; None = estimate instead."""
        return None

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "model": self.model, "simulated": self.simulated}
