"""OpenVINO GenAI backend (target: Intel Arc Pro B70 on Ubuntu).

Model directory must be an OpenVINO IR export, e.g.:
    optimum-cli export openvino --model Qwen/Qwen3-8B --weight-format int4 models/qwen3-8b-int4
Pipeline is built lazily on first use and runs in a worker thread.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from .base import Capabilities, LLMBackend, LLMError, LLMResult


class OpenVINOBackend(LLMBackend):
    name = "openvino"

    def __init__(self, model_path: str, device: str = "GPU"):
        self.model_path = model_path
        self.model = model_path
        self.device = device
        self._pipe = None
        self._lock = asyncio.Lock()

    def _build(self):
        try:
            import openvino_genai as ov_genai  # type: ignore
        except ImportError as e:
            raise LLMError("未安裝 openvino-genai（pip install openvino-genai）") from e
        try:
            # CACHE_DIR keeps the compiled GPU kernels, so only the first load pays the compile time.
            return ov_genai.LLMPipeline(self.model_path, self.device, CACHE_DIR=os.path.join(self.model_path, ".ov_cache"))
        except Exception as e:  # noqa: BLE001 - surface driver/model errors to the UI
            raise LLMError(f"OpenVINO 載入失敗（{self.device}）：{e}") from e

    async def _pipeline(self):
        async with self._lock:
            if self._pipe is None:
                self._pipe = await asyncio.to_thread(self._build)
            return self._pipe

    def _run(self, pipe, messages, temperature, max_tokens, json_schema):
        import openvino_genai as ov_genai  # type: ignore

        cfg = ov_genai.GenerationConfig()
        cfg.max_new_tokens = max_tokens
        cfg.do_sample = temperature > 0
        cfg.temperature = max(temperature, 1e-3)
        # The prompt below already went through the chat template; generate() would wrap it a second time by default.
        cfg.apply_chat_template = False
        if json_schema:
            # Constrained decoding (same role as Ollama's `format`): the output cannot leave the schema.
            cfg.structured_output_config = ov_genai.StructuredOutputConfig(json_schema=json.dumps(json_schema))
        # Qwen3: enable_thinking=False makes the template close the thinking block up front.
        prompt = pipe.get_tokenizer().apply_chat_template(messages, add_generation_prompt=True,
                                                          extra_context={"enable_thinking": False})
        out = pipe.generate([prompt], cfg)  # a list returns DecodedResults (with perf_metrics); a plain str returns str
        text = out.texts[0]
        usage: dict[str, Any] = {}
        try:
            pm = out.perf_metrics
            usage = {
                "prompt_tokens": pm.get_num_input_tokens(),
                "completion_tokens": pm.get_num_generated_tokens(),
                "ttft_ms": round(pm.get_ttft().mean),
                "tokens_per_sec": round(pm.get_throughput().mean, 1),
            }
        except Exception:  # noqa: BLE001 - metrics are nice-to-have
            pass
        return text, usage, None

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None) -> LLMResult:
        if any(m.get("images") for m in messages):   # never silently drop an image the caller relies on
            raise LLMError("OpenVINO 文字模型不支援影像輸入")
        pipe = await self._pipeline()
        t0 = time.perf_counter()
        try:
            text, usage, raw = await asyncio.to_thread(self._run, pipe, messages, temperature, max_tokens,
                                                     (meta or {}).get("json_schema"))
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"OpenVINO 推論失敗：{e}") from e
        return LLMResult(text=text, latency_ms=int((time.perf_counter() - t0) * 1000), usage=usage, raw=raw)

    async def capabilities(self) -> Capabilities:
        """LLMPipeline has no per-call context size (`context_tokens` is ignored): the limit is the exported model's
        max_position_embeddings. It is a text pipeline; images cannot be passed through this adapter."""
        limit = None
        try:
            with open(os.path.join(self.model_path, "config.json"), encoding="utf-8") as f:
                limit = int(json.load(f).get("max_position_embeddings") or 0) or None
        except (OSError, ValueError):
            pass
        return Capabilities(context_limit=limit, honors_context_tokens=False, vision=False,
                            source="openvino:config.json" if limit else "openvino:unknown")

    def count_tokens(self, messages: list[dict[str, str]]) -> int | None:
        """Exact count with the model's own chat template + tokenizer, once the pipeline is loaded."""
        if self._pipe is None:
            return None
        try:
            tok = self._pipe.get_tokenizer()
            prompt = tok.apply_chat_template(messages, add_generation_prompt=True, extra_context={"enable_thinking": False})
            return int(tok.encode(prompt).input_ids.get_shape()[-1])
        except Exception:  # noqa: BLE001 - fall back to the estimate
            return None

    async def health(self) -> dict[str, Any]:
        try:
            import openvino as ov  # type: ignore
        except ImportError:
            return {"ok": False, "detail": "未安裝 openvino / openvino-genai"}
        devices = ov.Core().available_devices
        if self.device not in devices and not any(d.startswith(self.device) for d in devices):
            return {"ok": False, "devices": devices, "detail": f"找不到裝置 {self.device}"}
        if not os.path.isfile(os.path.join(self.model_path, "openvino_model.xml")):
            return {"ok": False, "devices": devices, "detail": f"模型目錄不存在或不是 OpenVINO IR（缺 openvino_model.xml）：{self.model_path}"}
        return {"ok": True, "devices": devices, "detail": "裝置與模型目錄可用（首次推論時載入模型）"}
