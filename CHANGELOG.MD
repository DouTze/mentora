# Changelog

本文件記錄 mentora 的重要變更。

格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本編號採用 [Semantic Versioning](https://semver.org/lang/zh-TW/)。尚未正式發布的變更統一記錄於 `[Unreleased]`。規範見 `CONTRIBUTING.MD`。

## [Unreleased]

### 新增

- 新版學習流程（選用，`MENTOR_WORKFLOW=langgraph`）：教材範圍先抽成可追溯的來源區塊再由模型分組，正文由程式拼回；每段導讀經過分析、撰寫、程式驗證、對照原文檢查與必要時一次修訂，重大問題未解決的草稿不公開；學生操作改為可重送、可在後端重啟後接續的命令（`POST /api/sessions/{id}/events`）；新 session 固定模型設定與提示詞版本。預設仍是原本的流程。
- 導讀品質評估：固定教材集、人工評分規則與 `scripts/eval_reading.py`（舊版／新版對照、盲評包）。

- 教材庫：匯入 pdf／md／txt，以分類整理，依頁或行選定學習範圍。
- 逐段導讀與答疑，讀完後針對該段出題、提示、批改，並保存完整互動紀錄（可匯出 JSON／Markdown、用目前設定重跑）。
- 可切換的模型後端：示範模式（模擬）、Ollama、OpenVINO GenAI；選用以 pi 為基礎的閱讀 agent。
- 設定視窗：「偏好」可切換介面語系（繁體中文／English）與介面色彩（跟隨系統／亮／暗）；「伴讀」可挑選角色，每個角色綁一個本地模型並可調整生成參數。

### 修正

- Ollama 0.35 搭配 qwen3-vl 時導讀、切段與出題都輸出推理文字而失敗：qwen3 系列模型現在會先關閉 think 區塊，恢復受 JSON schema 約束的輸出。
