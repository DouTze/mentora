# 閱讀 agent（pi）

以 [pi](https://github.com/earendil-works/pi)（`@earendil-works/pi-agent-core` + `pi-ai`）為基礎的導讀 agent。後端在需要時把一個任務交給它：為一段寫導讀、回答學生對這段的疑問、或把讀完的一個部分壓成精簡筆記。它不會一次拿到整個閱讀範圍，而是用工具打開它需要的檔案。

```
agent/
  src/cli.ts         stdin 收一個 JSON 任務 → stdout 回一個 JSON 結果（後端的 PiReadingAgent 就是這樣呼叫它）
  src/agent.ts       建立 pi Agent：system prompt + 工具 + 任務提示，跑 agent loop，從最後回覆取出 JSON
  src/tools.ts       list_quotes / read_quote / read_guide / read_compact（唯讀）
  src/workspace.ts   讀後端寫出的 session 工作區
  src/prompts.ts     三種任務的提示詞（內容規範與 backend/app/prompts/{guide,ask,compact}.md 相同）
  src/model.ts       Ollama 模型與 provider 定義
  src/ollama-api.ts  pi 的自訂 API：直接打 Ollama 原生 /api/chat（think:false）
```

## 為什麼要切段、分部分、精簡

模型很小（4–8B、8–16k context），指定的範圍卻可能很長。做法（`backend/app/tutor/reading.py`）：

1. **段落（quote，來源單元）**：兩層。程式先切出候選段落（md/txt 以空行與 `#` 標題切；幾十字的過場句併入鄰段；沒有分段的 PDF 頁面在句尾切成小段），再由模型依 WaAi-ML 的來源單元規則把候選段落組成正式 quote（任務 `segment`）：一個段落或共同完成同一論述目的的一組段落；相關的定義、例子、平行項目不拆；公式與其引入、結論同單元；只在段落、小標題、獨立插入內容或論述轉折處開新單元；每個候選段落恰好用一次。後端驗證連續與覆蓋，不合法就退回候選段落。每段一個檔案。
2. **部分（part）**：連續的段落，合計不超過 `PART_CHARS`（5000 字），是模型一次讀得下的量。
3. **精簡筆記（compact）**：學生讀完一個部分，就把那個部分壓成一份筆記（定義、主張、關鍵詞），存成獨立檔案。
4. **出題**：範圍不長就用原文出題；超過 `GEN_TEXT_BUDGET`（12000 字）就只用各部分的精簡筆記，再加上導讀紀錄（每段重點＋學生問過的問題）。學生提問的地方會被優先出題。

## 工作區

後端為每次學習建立 `backend/data/reading/<session id>/`：

```
manifest.json     段落清單（不含原文）、部分、教材名稱與範圍
quotes/01.md      每段原文（檔名 = 段落編號）
guides/01.md      已產生的導讀（後端驗證後寫回）
parts/part-01.md  已讀完部分的精簡筆記
```

內建的 `inproc` agent 與這個 pi agent 讀寫同一套檔案，所以無論用哪一個，文件都在。

## 任務格式

stdin：

```json
{"task": "guide", "workspace": "/…/reading/<sid>", "index": 0, "prev_points": ["…"],
 "model": "qwen3-vl:8b", "host": "http://127.0.0.1:11434", "temperature": 0.2, "context_tokens": 16384}
{"task": "ask", "workspace": "…", "index": 2, "question": "…", "history": [{"quote_index": 0, "quote_label": "第 3 行", "question": "…", "answer": "…"}], …}   （history 含前幾段的問答）
{"task": "compact", "workspace": "…", "part": 0, "quote_to": 3, …}   （quote_to：提前出題時只壓縮讀到的段落）
{"task": "segment", "material_title": "…", "atoms": [{"heading": null, "text": "…"}, …], …}   （切段發生在工作區建立之前，不用工具）
```

stdout：`{"result": {…}, "model_raw": "…", "usage": {"input": n, "output": n}, "tool_calls": [{"name": "read_quote", "args": {"n": 1}}], "turns": 2}`，失敗時 `{"error": "…", "code": "model_unavailable" | "invalid_model_output"}`。`result` 的欄位與 `backend/app/tutor/reading.py` 的 `Guide` / `Answer` / `Compact` / `Segmentation` 相同，後端會再驗證一次。

## 啟用

```bash
cd agent && bun install
MENTOR_READING_AGENT=pi bun run dev      # 後端改把導讀任務交給 pi agent；模型設定需為 ollama
```

不設定時是 `inproc`：後端自己組提示詞呼叫目前的模型後端（mock / ollama / openvino），示範模式也能跑。

## 已知限制與設計筆記

- Ollama 的 OpenAI 相容端點（`/v1`）沒辦法關掉 Qwen3 的 thinking，qwen3-vl 會把 token 全花在思考上而沒有回答；所以 `ollama-api.ts` 直接打 `/api/chat` 並帶 `think: false`。非串流，一次請求一則回覆。
- 每個任務最多 8 個 turn（`max_turns`），避免小模型在工具上打轉。
- 工具全部唯讀，寫檔由後端負責（驗證 JSON 之後才寫）。
- 手動試跑：

  ```bash
  echo '{"task":"guide","workspace":"backend/data/reading/<sid>","index":0,"prev_points":[],"model":"qwen3-vl:8b","host":"http://127.0.0.1:11434"}' | bun run src/cli.ts
  ```
