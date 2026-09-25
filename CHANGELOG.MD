# Changelog

本文件記錄 mentora 的重要變更。

格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本編號採用 [Semantic Versioning](https://semver.org/lang/zh-TW/)。尚未正式發布的變更統一記錄於 `[Unreleased]`。規範見 `CONTRIBUTING.MD`。

## [Unreleased]

### 新增

- 教材庫：匯入 pdf／md／txt，以分類整理，依頁或行選定學習範圍。
- 逐段導讀與答疑，讀完後針對該段出題、提示、批改，並保存完整互動紀錄（可匯出 JSON／Markdown、用目前設定重跑）。
- 可切換的模型後端：示範模式（模擬）、Ollama、OpenVINO GenAI；選用以 pi 為基礎的閱讀 agent。
- 設定視窗：「偏好」可切換介面語系（繁體中文／English）與介面色彩（跟隨系統／亮／暗）；「伴讀」可挑選角色，每個角色綁一個本地模型並可調整生成參數。
