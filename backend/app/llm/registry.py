"""Holds the active model configuration and builds the matching backend."""
from __future__ import annotations

from .. import config
from ..models import ModelConfig
from .base import LLMBackend
from .mock import MockBackend
from .ollama import OllamaBackend
from .openvino import OpenVINOBackend


def default_config() -> ModelConfig:
    return ModelConfig(
        backend=config.DEFAULT_BACKEND,  # type: ignore[arg-type]
        model=config.OLLAMA_MODEL,
        host=config.OLLAMA_HOST,
        model_path=config.OPENVINO_MODEL_PATH,
        device=config.OPENVINO_DEVICE,
        **config.GEN_DEFAULTS,
    )


def build_backend(cfg: ModelConfig) -> LLMBackend:
    if cfg.backend == "ollama":
        return OllamaBackend(cfg.host or config.OLLAMA_HOST, cfg.model or config.OLLAMA_MODEL)
    if cfg.backend == "openvino":
        return OpenVINOBackend(cfg.model_path or config.OPENVINO_MODEL_PATH, cfg.device or "GPU")
    return MockBackend()


class Registry:
    def __init__(self, cfg: ModelConfig | None = None):
        self.cfg = cfg or default_config()
        self._backend: LLMBackend | None = None

    @property
    def backend(self) -> LLMBackend:
        if self._backend is None:
            self._backend = build_backend(self.cfg)
        return self._backend

    def update(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self._backend = None

    def snapshot(self) -> dict:
        """Everything a record needs to reproduce this run."""
        b = self.backend
        return {
            **self.cfg.model_dump(),
            "resolved_model": b.model,
            "simulated": b.simulated,
        }
