from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Raised when the backend cannot produce a response (connection, timeout, load failure)."""


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

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "model": self.model, "simulated": self.simulated}
