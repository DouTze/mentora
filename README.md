# mentora — 本機教學實驗平台（proto）

匯入自己的教材（pdf／md／txt）、用分類整理、選定頁數或行數，讓本機模型先**逐段導讀**、學生明白後再**針對那一段出題**教學，並保存完整互動紀錄。

```
backend/   Python + FastAPI + SQLite（切段、導讀、教學流程、模型呼叫、紀錄）
frontend/  Bun + React + TypeScript（學習畫面／學習紀錄／設定視窗）
agent/     以 pi 為基礎的閱讀 agent（舊版流程使用；舊版流程移除後，目前沒有接上）
```

## 啟動

需要 `uv` 與 `bun`。

```bash
bun run dev         # 後端 http://localhost:8000 ，前端 http://localhost:5173（/api 會代理到後端）
                    # Ctrl-C 一次關閉全部；再按一次（或 3 秒後）強制關閉。./dev.sh 等同此指令
bun run stop        # 關掉殘留在 8000 / 5173 的程序（例如終端機被直接關掉之後）；不會動 Ollama
```

### 開給區網夥伴試用

`bun run dev` 啟動時前端會綁定所有網卡並印出區網網址（例如 `http://192.168.20.103:5173`），把那個網址給同一個 Wi-Fi／區網的夥伴即可。後端與 Ollama 仍只在本機 `127.0.0.1`，夥伴的請求都經由前端的 `/api` 代理進來，不用另外開 8000。

- 第一次啟動 macOS 會跳「是否允許 bun 接受連入網路連線」，按允許；若沒跳出而夥伴連不上，到「系統設定 → 網路 → 防火牆」放行 bun，或先暫時關閉防火牆。
- 所有人共用同一個 SQLite 與教材庫：夥伴匯入的教材、學習紀錄、模型設定大家都看得到，也會互相蓋掉「伴讀」的模型設定（語系與介面色彩存在各自瀏覽器）。
- 模型一次只服務一個請求，多人同時答題會排隊等待。
- 只想自己用時：`HOST=127.0.0.1 bun run dev`。

`bun run dev` 也會順手管 Ollama：`11434` 沒有回應就幫你跑 `ollama serve`，結束時一起關掉；如果 Ollama 本來就開著（桌面程式、tool-box 在用），就直接沿用、結束時不動它。沒裝 Ollama 也能啟動，只是只能用示範模式。

分開跑：

```bash
cd backend && uv venv && uv pip install -e ".[dev]" -c requirements.lock && .venv/bin/uvicorn app.main:app --reload
cd frontend && bun install && bun run dev
```

預設是**示範模式**（`mock`）：用關鍵字規則模擬模型輸出，畫面會明確標示「模擬」。模型連線失敗時只會顯示錯誤並保留輸入，不會自動改用模擬。

## 畫面

左側是可收合的側邊欄（收合鈕在右上角，或按 `Cmd/Ctrl + \`），上半是首頁／進行中的學習／學習紀錄，中間是教材樹，最下方的齒輪開啟設定視窗（「偏好」：語系、介面色彩；「伴讀」：挑選角色，每個角色綁一個本地模型，進階設定裡調生成參數）。教材樹：

- **分類**：「教材」標題旁的資料夾鈕新增分類；點分類列可折疊（會記住）；滑過分類列有「匯入到這個分類」與「⋯」（重新命名／刪除，刪除後教材移到未分類）。
- **匯入**：上傳鈕、首頁的拖放區，或直接把檔案拖到某個分類上。教材列可以拖到別的分類搬移。
- **教材頁**：PDF 以「頁」、md／txt 以「行」為單位。PDF 文字用 PyMuPDF 依版面區塊擷取，區塊之間以空行分段、跨行的連字號會接回、頁首頁尾的頁碼會去掉，所以切段時拿到的是真正的段落（沒裝 PyMuPDF 時退回 pypdf，段落資訊較差）。點一下設定起點、再點一下設定終點（Shift＋點擊可延伸），或直接輸入數字；右側「學習」面板按「開始學習」。
- **導讀**：按「開始學習」後，範圍先被切成一段段的來源單元（quote）。切法分兩層：程式先依段落切出候選段落（過場短句併入鄰段、沒有分段的 PDF 頁面在句尾切成小段，`backend/app/tutor/reading.py` 的 `QUOTE_*`），再由模型依 WaAi-ML 精讀講義的來源單元規則（`prompts/segment.md`：一個段落或共同完成同一論述目的的一組段落；相關的定義、例子不拆；公式與其引入、結論同單元；只在段落、小標題、獨立插入內容或論述轉折處開新單元）把候選段落組成正式的 quote。模型輸出不合法時退回候選段落，畫面會標示「依段落切成」。`MENTOR_QUOTE_PLANNER=rules` 可完全關掉模型切段。每段顯示對應原文的位置與節錄、（原文非中文時的）中文課文，以及取代原文的主要正文——用教材本身的聲音直接講，不是站在書外轉述，也不會叫你回去看原文；讀完可以直接發問（導師會參考這次學習到目前為止的問答，能承接你在前幾段問過的問題），「我明白了」才進下一段；也可以按「先出題」，只就讀過的段落出題。前一段讀完時會先偷跑下一段的導讀。切段、導讀、整理筆記、出題都是後端的背景工作：請求立刻回來，畫面每 2 秒輪詢學習狀態並顯示目前階段，所以不會因為模型跑幾分鐘而斷線。
- **出題**：所有段落導讀完成後，模型依原文（或長範圍的精簡筆記）加上導讀紀錄（每段重點＋學生問過的問題）產生 1–3 個概念、每個概念一題診斷＋一題新情境；學生提問過的地方會優先出題。之後的流程與規則和示範教材相同。答題時可「對照範圍原文」。

導讀、答疑、題目與導師回饋都以 Markdown 子集顯示，數學用 KaTeX（行內 `$…$`、獨立 `$$…$$`），由 `frontend/src/components/RichText.tsx` 渲染；模型輸出不會被當成 HTML。

### 長範圍怎麼讀

範圍切段後再依原書結構分成**閱讀部分**（`backend/app/reading/segmentation.py`）。讀完一個部分，後端從已核對的命題整理出筆記；出題只依據已讀且已核對的命題與筆記。一次學習的上限是 60,000 字（`backend/app/materials.py` 的 `MAX_RANGE_CHARS`），限制的是時間而不是 context。切段、導讀與筆記另外鏡像成 Markdown 檔，放在 `backend/data/reading/<session>/`（`quotes/`、`guides/`、`parts/`）。掃描型 PDF（沒有文字層）目前會被拒絕。學習紀錄會保留每一輪的題目原文、導讀時的每段導讀與提問。

### 學習流程

所有教材學習都由 `backend/app/workflows/`（LangGraph）執行。舊版流程（`session.py` 背景工作、`/reading/*` 端點、`MENTOR_WORKFLOW`、`MENTOR_READING_AGENT`）已移除；舊版流程留下的紀錄仍可檢視與匯出，已進入出題的可以繼續作答，還在導讀中的無法繼續，需重新建立學習。流程的重點：

- **切段**：程式先把範圍抽成可追溯的來源區塊（段落、標題、清單項目、整段程式碼、`$$` 公式、跨頁的句子…），模型只回傳區塊編號與理由，正文一律由程式從區塊拼回，不會被模型改寫或漏掉。公式與其引入／where 說明、同一清單、跨頁句子等由程式標示為不可切開；模型的分組不合法時修正一次，仍不合法就退回規則切法。
- **原書結構**：Markdown 的 `#`–`###` 視為正式小節，一個閱讀部分（part）最多一個第三層小節；完整論證（第一個前提…因此…）、例子與它說明的概念、程式碼與說明、公式與 where 說明不會被切開（太長時拆成相鄰單元，但留在同一部分並帶上前一段當上下文）。PDF 以版面猜到的短標題不當成正式小節。
- **導讀（先核對來源，再寫教學）**：每段依序
  1. 程式把原文逐句編號，模型列出依據哪幾句的命題（外文另逐句翻成中文課文）；程式核對每一句都被涵蓋、條件／例外／否定／量詞沒有掉、公式原樣保留，外文再由模型逐句核對譯文（**Gate 1**）。
  2. 依讀者狀態規劃最少必要的教學橋梁（可以沒有）；閱讀深度、讀者背景與規劃理由只控制寫法，不會寫進正文。
  3. 撰寫主要正文，再由程式與模型只看正文逐項核對每個命題與橋梁（**Gate 2**）；中文課文寫到了不算正文寫到，「補充說明」標籤不能免除檢查，文字相似不能撤回重大問題。
  4. 有問題時修訂一次；撤回疑點必須提出程式找得到、條件與公式都保留的正文原句。重大問題或必要證據未解決時，草稿**不公開**，畫面顯示白話原因（例如「需要確認公式」）與可否重試。
  
  每段最多 14 次模型呼叫（含格式修正與修訂），重啟不重置；每次呼叫的輸入估計、輸出保留與實際放入的上下文都記在 `llm_calls`（`GET /api/sessions/{id}/trace`）。通過 Gate 1 的來源命題會依來源、符號證據、提示詞與模型版本快取，可在其他 session 重用；正文依讀者狀態而定，不共用。
- **輸入預算**：每次呼叫都檢查「輸入估計 + 輸出保留 + 餘量 ≤ 模型實際可用長度」（Ollama 由 `/api/show` 取得、OpenVINO 讀模型的 `max_position_embeddings`，再以 `MENTOR_MAX_CONTEXT_TOKENS` 為上限）。超出時先捨棄非必要的上下文（以前的問答、已讀重點），必要的原文、命題與橋梁絕不截斷；仍放不下就停下來說明，不會悄悄截掉條件。Ollama 的 `num_ctx` 固定在 `MENTOR_GRAPH_NUM_CTX` 的級距，避免每次呼叫都重新載入模型。
- **PDF 符號**：匯入 PDF 時保存每段的頁碼、版面位置、字型與粗體／斜體／上標線索。含數學符號的段落只在 session 的模型能看圖（例如 Ollama 的 qwen3-vl）時，送出那一小塊版面截圖請模型判讀，並與字型線索交叉比對；不能看圖或判讀矛盾時保留為「需要確認公式」，不從純文字猜。截圖存在 `backend/data/evidence/`。
- **筆記與出題**：讀完一部分時，筆記只從已通過 Gate 1 的命題壓縮，每條標明依據並檢查條件與公式是否保留，再由模型逐條核對；沒有筆記涵蓋的已核對命題原樣保留。出題先由程式依已讀部分排出主題（學生問過的優先），每個概念與題目都要標出依據，經核對可由依據回答且條件適用才採用；已讀範圍沒有通過核對的內容時不出題。
- **閱讀設定**：`POST /api/sessions` 可帶 `reading_depth`（`close` 細讀／`skim` 略讀）、`reading_goal`、`reader_background`；沒帶的欄位記為預設。
- **可恢復**：學生的每個操作是一個帶 `command_id` 的命令，先寫進 SQLite 才回應；後端重啟會接續已受理的命令，同一個命令重送只會套用一次。流程位置存在 `backend/data/checkpoints.sqlite3`（LangGraph checkpoint），學習紀錄仍以 `mentor.sqlite3` 為準。
- **固定設定**：新 session 保存模型設定與所有提示詞的全文（prompt bundle），之後改設定或改提示詞只影響新 session。

一次只能有一個後端程序執行學習流程（`runner_lease`）；第二個程序可以檢視紀錄，但送出操作會回 503。

重啟與回退：

- 重啟：直接重啟後端即可，啟動時會接續未完成的命令；等待學生操作的 session 不會自己前進，失敗的工作等使用者按「重試」。
- 舊 session：在導讀流程升級（reading-graph-v1 → v2）前建立的 LangGraph session，下一次操作前會自動升級一次並記在匯出的「流程升級」：已公開的導讀保持原樣並標明是舊版檢查，之後的段落使用來源核對與正文核對，原有的閱讀部分不重新切分；用舊版讀過的段落出題時只以原文為依據。
- 回退：若要退回舊版程式，先停掉後端，成對還原備份的 `mentor.sqlite3` 與 `checkpoints.sqlite3`（備份之後的資料會遺失）。不要把升級過的 `mentor.sqlite3` 直接交給舊版程式：舊版建立 session 時用位置式的 `INSERT`，遇到新增欄位會失敗。
- 備份：停掉後端後，一起複製 `backend/data/mentor.sqlite3`、`checkpoints.sqlite3` 與 `reading/`。

## 測試

```bash
cd backend && .venv/bin/python -m pytest     # e2e 流程：切段／導讀／答疑／筆記／答對／答錯／理由矛盾／輸出不合法／連線中斷／重啟續接／重跑／匯出
                                             # 新版流程：來源完整性、adapter 保真、預算、子流程、命令去重、真子 process 崩潰後恢復
                                             # 閱讀方法：放行漏洞回歸、Gate 1／Gate 2、輸入預算、原書結構、PDF 符號證據、筆記／出題、舊資料相容
cd frontend && bun test && bun run typecheck  # 畫面測試：以假 fetch 隔離後端
```

### 導讀品質評估（選用，需要本機模型）

固定教材集與人工評分規則在 `backend/evals/reading/`（`manifest.json`、`RUBRIC.md`）。評估腳本跑 app 使用的導讀流程，結果寫到 `backend/data/evals/`；舊版流程已移除，`data/evals/` 內保存的舊版結果（例如 `r8-legacy`）仍可用 `compare` 對照：

```bash
cd backend
.venv/bin/python scripts/eval_reading.py candidate --backend ollama --model qwen3-vl:4b    # 導讀流程
.venv/bin/python scripts/eval_reading.py compare data/evals/<A> data/evals/<B>              # 指標對照 + 盲評包
.venv/bin/python scripts/runtime_smoke.py --backend ollama --model qwen3-vl:4b             # LangChain bridge 最小真模型檢查
.venv/bin/python scripts/r8_report.py legacy=data/evals/<A> graph=data/evals/<B>            # 公開率、未公開原因、延遲、各節點 tokens、估計／實際 tokens
.venv/bin/python scripts/vision_probe.py --model qwen3-vl:4b                               # PDF 符號：模型能否從版面截圖讀出粗體／上下標
.venv/bin/python scripts/prefetch_probe.py --model qwen3-vl:4b --read-seconds 20           # 預取開／關時讀者實際等待與背景呼叫成本
.venv/bin/python scripts/session_probe.py --model qwen3-vl:4b --out data/evals/<dir>       # 用真實 app 讀完教材：筆記核對、出題依據與抽查表
```

`--stages segment` 只評切段（快）；`--cases` 指定教材。腳本預設只用本機模型，不會下載模型或把教材送到雲端。自動檢查只是信號，品質以 `RUBRIC.md` 的人工盲評為準：盲評包先只呈現兩份主要正文，中文課文另外評分。放行漏洞的回歸 fixtures 在 `backend/evals/reading/regressions/`，自製 PDF fixtures 在 `backend/evals/reading/pdf/`。

### 遠端推論（評估腳本，選用）

app 本身要用遠端模型時，見「接真實模型 → 遠端 Qwen」。評估腳本要驗證 prompt 與流程在 qwen 模型下的運作成果（切段、導讀、關卡、筆記與題目），但本機跑一輪太慢時，可以只把模型呼叫送到 Colab GPU 上的同一個 Ollama 模型。其他步驟（切段規則、關卡、LangGraph、資料庫）仍在本機執行。步驟：

1. 在 Colab 上傳 `backend/scripts/colab/mentor_ollama_remote.ipynb`，選 GPU 執行環境，按「全部執行」。斷線時再按一次即可（token 不變，網址會換）。
2. 評估腳本會自動比對遠端與本機的模型 digest；本機 Ollama 沒啟動時，改讀本機 manifest 檔。
3. 把 notebook 印出的整段設定存成本機檔案（例如 `remote.env`），`source` 後執行評估：`--host $REMOTE_URL --jobs $REMOTE_JOBS`。

限制與防護：

- 驗收範圍：遠端 digest 與本機相同時是同一個模型，品質結果（自動檢查與人工盲評）可以驗收，`run_class` 為 `remote-quality`。digest 不同或無法比對時為 `remote-unverified`，只供開發參考。耗時與效能只認本機循序執行（`local-acceptance`）；平行或遠端的耗時不能比較。`results.json` 的 `provenance` 記錄主機、兩邊的 digest、seed 與平行數，報告開頭也會標出。
- 只送自製評估教材。adapter 遇到網際網路上的主機（公開 IP 或公開網域，例如 tunnel 網址）時，必須設定 `MENTOR_ALLOW_REMOTE_MODEL=1` 才會送出請求。
- proxy token 只從環境變數 `MENTOR_OLLAMA_TOKEN` 讀取，不寫入設定、session 或評估紀錄。proxy 只轉送 `/api/chat`、`/api/show`、`/api/tags`、`/api/version`。
- `MENTOR_OLLAMA_SEED` 可固定 sampling seed。Colab（CUDA）與本機（Metal）即使 seed 相同，輸出仍可能不同，品質判斷看多次執行的分佈與人工盲評。
- 不要 `export OLLAMA_HOST`：本機的 `ollama` 指令也讀這個變數。

## 結構

| 檔案 | 內容 |
|---|---|
| `backend/app/materials.py` | 匯入教材：分類、檔案、文字擷取（PDF→頁、md/txt→行）、範圍取文 |
| `backend/app/tutor/reading.py` | 導讀共用的小工具：初始閱讀狀態、中文判斷、節錄、問答歷史 |
| `backend/app/tutor/reading_agent.py` | session 的 Markdown 鏡像檔（`data/reading/<session>/`） |
| `backend/app/tutor/generate.py` | 出題結果轉成與內建教材相同的結構 |
| `agent/` | pi 閱讀 agent（舊版流程使用，目前沒有接上） |
| `backend/app/content/*.json` | 內建示範教材：概念、解釋、例子、題目、評分依據、提示 |
| `backend/app/strategies/*.json` | 教學規則參數（提示次數、追問次數、帶入模型的近期輪數） |
| `backend/app/prompts/assess.md` | 模型提示詞（與規則分開） |
| `backend/app/tutor/engine.py` | 狀態機：模型判斷 → 規則 → 下一步，純函式 |
| `backend/app/tutor/session.py` | 內建教材的作答、所有 session 的檢視、重跑 |
| `backend/app/llm/` | `mock` / `ollama` / `remote`（遠端 Ollama，經 token proxy）/ `openvino` 後端，同一介面 |
| `backend/app/db.py` | SQLite：sessions（含教材／策略／模型設定快照）、turns、settings；`schema_migrations` 依序升級 |
| `backend/app/llm/langchain_bridge.py`、`runtime.py`、`tasks.py` | LangChain 接到既有 adapter；不可變模型快照、共用呼叫預算、呼叫紀錄；結構化任務與 schema 約束的工具選擇 |
| `backend/app/reading/` | 新版流程的來源區塊、切段、導讀契約、唯讀工具、prompt bundle |
| `backend/app/workflows/` | LangGraph 導讀子流程、學習流程、命令驗證、單一 worker runner |
| `backend/app/persistence/repository.py` | 來源快照、產物、命令、呼叫紀錄與原子提交 |
| `backend/evals/`、`backend/scripts/eval_reading.py` | 固定教材集、gold labels、評估與盲評 |

### 流程

開始學習 → 診斷題 → 回饋／提示／追問 → 新情境練習 → 學習小結。每個概念各一題診斷、一題新情境。

| 模型判斷 | 規則 | 動作 |
|---|---|---|
| correct + reasoning_ok | 答對且理由合理 | `advance`（診斷→新情境）／`pass`（新情境完成） |
| correct + !reasoning_ok | 答對但理由有誤 | `probe` 追問 |
| partial / incorrect | 答錯或不懂 | `hint`，提示用完 → `explain` 顯示教材並標記需要複習 |
| unclear | 無法判斷 | `probe`，追問超過上限 → 當作 `hint` |

模型只回傳結構化 JSON（`assessment / reasoning_ok / evidence / feedback / hint / probe / source_ids`）；後端驗證欄位、過濾不存在的教材引用、無效輸出重試一次後回報錯誤。

### 紀錄

每輪保存：題目、學生回答、送給模型的完整訊息、模型原始輸出、驗證備註、判斷、觸發的規則與動作、導師訊息、前後狀態、耗時、token 用量、錯誤、後端與模型。Session 保存教材、策略與模型設定的快照。可匯出 JSON／Markdown，可「用目前設定重跑」同一批回答建立新紀錄。

## API

```
GET  /api/health
GET  /api/content, /api/content/{id}, /api/strategies
GET  /api/settings   PUT /api/settings   POST /api/settings/check
PUT  /api/settings/remote   {"url","token"?}  # 遠端模型伺服器；token 只存在後端、不會回傳，留空沿用
GET  /api/companions PUT /api/companions   # 伴讀角色與各自的模型設定；PUT 會把 active 角色的設定套用成目前模型
GET  /api/library                          # 分類 + 教材清單
POST /api/categories   PATCH|DELETE /api/categories/{id}
POST /api/materials (multipart: file, category_id)
GET|PATCH|DELETE /api/materials/{id}       GET /api/materials/{id}/file
POST /api/sessions   {"material_id","start","end","reading_depth"?,"reading_goal"?,"reader_background"?} 或 {"content_id"}
GET  /api/sessions   GET /api/sessions/{id}
POST /api/sessions/{id}/answer   {"answer": "..."}   # 內建教材 session；教材學習改用 events
POST /api/sessions/{id}/events   {"command_id","type":"guide|ask|next|finish|answer|retry","expected_revision"?,"unit_id"?,"question_id"?,"payload"}
                                 # 教材學習的所有操作：202 已受理／200 已完成的重送／409 過期或忙碌（附最新 session）
GET  /api/sessions/{id}/commands/{command_id}   # {status, stage, result, error, retryable}
GET  /api/sessions/{id}/trace                    # 開發用：每次模型呼叫與命令紀錄
POST /api/sessions/{id}/replay
GET  /api/sessions/{id}/export?format=json|md
```

## 接真實模型

- **遠端 Qwen（Colab 或其他伺服器）**：讓夥伴不必在自己電腦跑模型也能試用。
  1. 在 Colab 上傳 `backend/scripts/colab/mentor_ollama_remote.ipynb`，選 GPU，按「全部執行」；最後會印出網址與 token。其他伺服器要跑同一個 token proxy（`backend/scripts/colab/ollama_proxy.py`）在 Ollama 前面。
  2. 到「設定 → 伴讀」，後端選「遠端 Qwen」、模型填 `qwen3-vl:4b`，在「遠端模型伺服器」填網址與 token，按「儲存連線」，再儲存角色設定、按「檢查連線」。
  3. Colab 重開後網址會換：只要在設定裡更新網址（token 不變可留空），進行中的學習會接著用新網址。
  - 選擇這個後端，代表同意把教材內容送到那台伺服器。token 只存在後端的設定裡，不會出現在設定回應、學習紀錄、呼叫紀錄或匯出；學習紀錄只記後端是 `remote` 與模型名稱。
  - 不在本機的模型，耗時取決於網路與遠端 GPU，不能和本機的耗時比較。
- **Ollama（已實測，macOS／Ollama 0.34）**：`bun run dev` 會順手啟動 Ollama；到「設定 → 伴讀」挑角色、後端選 Ollama、填模型名稱後儲存，再按「檢查連線」。預設沿用 tool-box 的 `http://127.0.0.1:11434`。
  - `qwen3-vl:8b`：建議。出題 10–25 秒、判讀每次 2–5 秒；會照範圍涵蓋多個主題，提示是引導式問句。
  - `qwen3-vl:4b`：快一倍（出題 5–15 秒），但同一範圍有時只出 1 個概念、提示常直接講出答案。
  - Ollama 0.35 起 qwen3-vl 不理會 `think: false`（推理文字跑進 `thinking`、JSON schema 也失效）；adapter 對 qwen3 系列在訊息最後補一則已關閉 think 區塊的 assistant 訊息，讓輸出恢復成受 schema 約束的 JSON（macOS／Ollama 0.35.1 實測，qwen3-vl:4b 每段導讀約 7 秒）。
  - 呼叫使用 `/api/chat`、`think: false`，`format` 傳 **JSON schema**（不是 `"json"`）：純 JSON 模式下 qwen3-vl:4b 約一半機率卡在重複 token，Ollama 回 500 `token repeat limit reached`；改用 schema 後連續 20 多次出題沒有再發生。qwen3-vl 會把答案放在 `thinking` 欄位、`content` 留空，adapter 兩個都讀。
  - 出題時 `max_tokens` 至少 4096、context 至少 16384（`session.py` 的 `GEN_*`），不受設定視窗較小的數值限制；2048 會把三個概念的 JSON 截斷。
- **OpenVINO GenAI（macOS CPU 已實測 openvino-genai 2026.4；B70 GPU 尚未實機驗證）**：
  ```bash
  cd backend && uv pip install -e ".[openvino]" huggingface_hub
  .venv/bin/hf download OpenVINO/Qwen3-8B-int4-ov --local-dir models/qwen3-8b-int4-ov   # 已轉好的 IR，約 5 GB，不需要 optimum-cli
  .venv/bin/python scripts/ov_smoke.py --model-path models/qwen3-8b-int4-ov --device GPU   # 到指定機器先跑這個
  ```
  `ov_smoke.py` 用和 app 相同的提示詞與解析器檢查：裝置、模型載入、出題 JSON、三種回答的判讀，並印出 tok/s；全過才回傳 0。`devices` 沒列出 `GPU` 代表驅動（intel-opencl-icd／level-zero）沒裝好，可先用 `--device CPU` 確認其餘部分。通過後到「設定 → 伴讀」選 OpenVINO、填模型目錄與裝置，或用 `MENTOR_BACKEND=openvino OPENVINO_MODEL_PATH=... OPENVINO_DEVICE=GPU` 啟動。
  - 出題與判讀都用 `StructuredOutputConfig(json_schema=...)` 約束輸出（等同 Ollama 的 `format`）；Qwen3-8B 實測出題 2 次、判讀 3 次 JSON 全部合法。
  - 提示詞自己套 chat template（`enable_thinking=False`），所以 `apply_chat_template` 要關掉，否則 `generate()` 會再包一層。
  - 編譯後的 kernel 快取在 `<模型目錄>/.ov_cache`，第二次載入起會快很多。M4 Pro CPU 只有 3–7 tok/s（出題約 4 分鐘），只適合驗證流程。

## 參與開發

分支、commit 與版本規範見 [CONTRIBUTING.MD](CONTRIBUTING.MD)，變更紀錄見 [CHANGELOG.MD](CHANGELOG.MD)。

## 介面語系（i18n）

介面字串集中在 `frontend/src/i18n/`：`zh-Hant.shell.ts`（側邊欄、設定視窗、角色）與 `zh-Hant.pages.ts`（各頁面）是來源，`en.ts` 由 codex 翻譯產生。新增或修改中文字串後在 `frontend/` 跑 `bun run i18n:translate`，只會把缺少或中文已變動的 key 送去翻譯（`--all` 全部重翻），翻譯前後會檢查 `{placeholder}` 是否一致。英文缺 key 時自動退回中文。後端回傳的內容（錯誤訊息、模型輸出、教材）不受語系影響。

