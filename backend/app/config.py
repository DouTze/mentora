"""Runtime configuration. Everything here can be overridden by env vars."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MENTOR_DATA_DIR", APP_DIR.parent / "data"))
DB_PATH = Path(os.environ.get("MENTOR_DB_PATH", DATA_DIR / "mentor.sqlite3"))

CONTENT_DIR = APP_DIR / "content"
STRATEGY_DIR = APP_DIR / "strategies"
PROMPT_DIR = APP_DIR / "prompts"

# Default model settings (can be changed at runtime from the settings page).
DEFAULT_BACKEND = os.environ.get("MENTOR_BACKEND", "mock")  # mock | ollama | openvino
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-vl:4b")  # same model the local tool-box serves
OPENVINO_MODEL_PATH = os.environ.get("OPENVINO_MODEL_PATH", "./models/qwen3-8b-int4")
OPENVINO_DEVICE = os.environ.get("OPENVINO_DEVICE", "GPU")

# 導讀 agent：inproc（在後端直接組提示詞呼叫模型）| pi（交給 ../agent 的 pi 閱讀 agent，需 bun 與 ollama）
READING_AGENT = os.environ.get("MENTOR_READING_AGENT", "inproc")
PI_AGENT_DIR = Path(os.environ.get("MENTOR_PI_AGENT_DIR", APP_DIR.parent.parent / "agent"))
READING_PREFETCH = os.environ.get("MENTOR_READING_PREFETCH", "1") != "0"  # 產生目前段落的導讀後，先偷跑下一段
# 切段：model（由模型依來源單元規則把候選段落組成 quote）| rules（只用規則切法）
QUOTE_PLANNER = os.environ.get("MENTOR_QUOTE_PLANNER", "model")
READING_DIR = Path(os.environ.get("MENTOR_READING_DIR", DATA_DIR / "reading"))

GEN_DEFAULTS = {
    "temperature": 0.2,
    "max_tokens": 512,
    "context_tokens": 8192,
}
