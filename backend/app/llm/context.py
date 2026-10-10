"""One budget entry for every model call (R2).

A call fits when

    estimated_input + reserved_output + margin <= effective_limit

- estimated_input counts the whole request as sent: every message (instructions, source, history, tool results,
  format-fix turns) plus the chat-template overhead and, for Qwen3 on Ollama, the closed think block. JSON schemas
  are not counted: both real adapters use constrained decoding (Ollama `format`, OpenVINO StructuredOutputConfig),
  which does not add the schema to the prompt. A backend with its tokenizer loaded (OpenVINO) gives the exact count;
  otherwise a versioned, deliberately conservative estimate is used and named in the trace.
- reserved_output is the max_tokens of that call.
- effective_limit is what the loaded model can take (Capabilities.context_limit) capped by MENTOR_MAX_CONTEXT_TOKENS.
  For Ollama the call's num_ctx is then chosen to hold the request (in stable buckets, so the runner is not
  reloaded for every size); OpenVINO ignores the per-call size, so only the model limit applies. Raising num_ctx
  above what the model supports is never used to "make room".

A prompt is assembled from ContextItems. Required items (the unit's source text, the claims under check …) are
never cut; optional ones (earlier key points, older Q&A …) are dropped lowest priority first, each replaced by an
explicit "not provided" marker. If the required items alone do not fit, ContextOverflow says so — nothing is
silently truncated. The PacketRecord of what was included, dropped and why goes into the call trace."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .. import config

TOKEN_METHOD = "heuristic-v2(cjk=1.0,other=0.35,msg=8,base=24,image=1280)"
IMAGE_TOKENS = 1280      # one rendered region (≤ ~1000×300 px at 28 px patches) with margin
MESSAGE_OVERHEAD = 8      # role markers / separators of one chat-template turn
BASE_OVERHEAD = 24        # generation prompt + closed think block + BOS
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯　-〿]")


def estimate_tokens(text: str) -> int:
    """Conservative for Qwen-family BPE: one token per CJK char / full-width punctuation, 0.35 per other char
    (Latin text runs about 0.25–0.3). Measured against Ollama's prompt_eval_count in the eval report."""
    t = text or ""
    cjk = len(CJK_RE.findall(t))
    return cjk + math.ceil((len(t) - cjk) * 0.35)


def chars_per_token(text: str) -> float:
    """Characters per estimated token of a text (about 1 for CJK, about 3 for Latin): converts a length bound set in
    CJK characters into the same length in the text's own language."""
    t = text or ""
    return max(1.0, len(t) / max(1, estimate_tokens(t)))


def estimate_messages(messages: list[dict]) -> int:
    return BASE_OVERHEAD + sum(MESSAGE_OVERHEAD + estimate_tokens(m.get("content") or "")
                               + IMAGE_TOKENS * len(m.get("images") or []) for m in messages)


def margin_for(estimated_input: int) -> int:
    """Head-room for estimation error: 256 tokens + 10 % of the input. Against Ollama's prompt_eval_count
    (qwen3-vl:4b, R8) the estimate ran 0.92–1.37× the actual count; LaTeX-heavy prompts were up to 8 % low."""
    return 256 + math.ceil(estimated_input * 0.10)


class ContextOverflow(Exception):
    def __init__(self, detail: str, record: dict | None = None):
        super().__init__(detail)
        self.detail, self.record = detail, record or {}


@dataclass(frozen=True)
class ContextLimits:
    """The limits of one runtime, resolved from the backend's capabilities."""
    limit: int                 # effective input + output limit
    honors_context_tokens: bool
    floor: int                 # num_ctx used when the request is smaller (stable bucket)
    source: str

    def num_ctx(self, need: int) -> int:
        if not self.honors_context_tokens:
            return self.floor
        bucket = max(self.floor, math.ceil(need / 4096) * 4096)
        return min(bucket, self.limit)


def resolve_limits(caps, session_context_tokens: int, task_floor: int = 0) -> ContextLimits:
    ceiling = config.MAX_CONTEXT_TOKENS
    model = caps.context_limit or ceiling
    limit = min(model, ceiling) if caps.honors_context_tokens else model
    floor = min(limit, max(session_context_tokens, task_floor, config.GRAPH_NUM_CTX))
    return ContextLimits(limit=limit, honors_context_tokens=caps.honors_context_tokens, floor=floor,
                         source=f"{caps.source};ceiling={ceiling}")


def check_fit(messages: list[dict[str, str]], reserved_output: int, limits: ContextLimits,
              exact: int | None = None) -> dict:
    est = exact if exact is not None else estimate_messages(messages)
    margin = 0 if exact is not None else margin_for(est)
    need = est + reserved_output + margin
    return {"estimated_input": est, "method": "tokenizer" if exact is not None else TOKEN_METHOD,
            "reserved_output": reserved_output, "margin": margin, "need": need, "limit": limits.limit,
            "num_ctx": limits.num_ctx(need), "fits": need <= limits.limit, "limit_source": limits.source}


# -- packets ---------------------------------------------------------------------

class ContextItem(BaseModel):
    """One field of a prompt. `required` items are never cut; optional ones may be dropped whole."""
    key: str                                  # template field name ({{key}})
    text: str
    required: bool = True
    priority: int = 0                         # optional items with the lowest priority are dropped first
    source_ids: list[str] = Field(default_factory=list)
    version: str | None = None
    complete: bool = True                     # False: the item is a declared part of something larger
    note: str = ""                            # e.g. which units were not provided
    dropped_text: str = "（因輸入長度限制，這部分沒有提供）"


class PacketRecord(BaseModel):
    node: str
    items: list[dict] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    fit: dict = Field(default_factory=dict)


def render(template: str, fields: dict[str, str]) -> str:
    out = template
    for k, v in fields.items():
        out = out.replace("{{" + k + "}}", v)
    return out


@dataclass
class Packet:
    node: str
    template: str
    items: list[ContextItem]
    fixed: dict[str, str] = field(default_factory=dict)    # short fields (titles, positions) – always included

    def build(self, reserved_output: int, limits: ContextLimits) -> tuple[str, PacketRecord]:
        """Render with every item; drop optional items (lowest priority first) until the call fits."""
        dropped: set[str] = set()
        optional = sorted((i for i in self.items if not i.required), key=lambda i: i.priority)
        while True:
            fields = {**self.fixed, **{i.key: (i.dropped_text if i.key in dropped else i.text) for i in self.items}}
            msg = render(self.template, fields)
            fit = check_fit([{"role": "user", "content": msg}], reserved_output, limits)
            if fit["fits"]:
                break
            nxt = next((i for i in optional if i.key not in dropped), None)
            if nxt is None:
                req = [f"{i.key}≈{estimate_tokens(i.text)}" for i in self.items if i.required]
                raise ContextOverflow(f"{self.node}：必要內容（{', '.join(req)} tokens）加上輸出保留 {reserved_output} "
                                      f"超過模型可用的 {limits.limit} tokens", fit)
            dropped.add(nxt.key)
        rec = PacketRecord(node=self.node, dropped=sorted(dropped), fit=fit, items=[
            {"key": i.key, "required": i.required, "included": i.key not in dropped, "source_ids": i.source_ids,
             "version": i.version, "complete": i.complete, "note": i.note, "tokens": estimate_tokens(i.text)}
            for i in self.items])
        return msg, rec
