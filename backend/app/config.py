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

READING_PREFETCH = os.environ.get("MENTOR_READING_PREFETCH", "1") != "0"  # 產生目前段落的導讀後，先偷跑下一段
# 一個區塊的導讀沒通過檢查時，自動重新產生（新的嘗試、新的預算），連同第一次最多幾次；重來也不會成功的原因不重試
READING_BLOCK_ATTEMPTS = int(os.environ.get("MENTOR_BLOCK_ATTEMPTS", "3"))
# 切段：model（由模型依來源單元規則把候選段落組成 quote）| rules（只用規則切法）
QUOTE_PLANNER = os.environ.get("MENTOR_QUOTE_PLANNER", "model")
CHECKPOINT_PATH = os.environ.get("MENTOR_CHECKPOINT_PATH", "")   # 預設與業務 DB 同目錄的 checkpoints.sqlite3
# 首頁書櫃：掃描索引、封面與上次挑書結果放這裡；SHELF_WARMUP=0 時啟動不先掃描（測試）
SHELF_DIR = Path(os.environ.get("MENTOR_SHELF_DIR", DATA_DIR / "shelf"))
SHELF_WARMUP = os.environ.get("MENTOR_SHELF_WARMUP", "1") != "0"
READING_DIR = Path(os.environ.get("MENTOR_READING_DIR", DATA_DIR / "reading"))

GEN_DEFAULTS = {
    "temperature": 0.2,
    "max_tokens": 512,
    "context_tokens": 8192,
}

# Context budget of the LangGraph workflow (app/llm/context.py). MAX_CONTEXT_TOKENS caps what a call may use even
# when the model supports more (memory); GRAPH_NUM_CTX is the stable Ollama num_ctx bucket (changing num_ctx between
# calls reloads the model runner).
MAX_CONTEXT_TOKENS = int(os.environ.get("MENTOR_MAX_CONTEXT_TOKENS", "32768"))
GRAPH_NUM_CTX = int(os.environ.get("MENTOR_GRAPH_NUM_CTX", "16384"))
