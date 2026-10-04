# 導讀 Agent 與 LangGraph 改造進度

對應規劃：[reading-agent-langgraph-plan.md](reading-agent-langgraph-plan.md)。本文件記錄每階段的實際完成項、檢查結果、決策與限制。未執行的檢查不標成通過。

2026-10-04 新增 [WaAi-ML 閱讀方法落實規劃](reading-method-alignment-plan.md) 與更新後的 [Claude Code 交接指令](claude-code-reading-agent-handoff.md)。同日依 R0–R8 接續既有成果，結果見「下一輪閱讀方法落實」；下方 P0–P6 保留為第一輪紀錄。

## 狀態總覽

| 階段 | 狀態 | 摘要 |
| --- | --- | --- |
| P0 baseline／fixture／評估契約 | 完成 | 13 份教材、45 個標註單元、3 個損壞來源；baseline 已記錄並區分既有失敗 |
| P1 runtime／LangChain bridge | 完成 | 依賴 lock、bridge、snapshot、budget、trace；Ollama 真模型通過；OpenVINO CPU 通過、GPU 未驗證 |
| P2 來源單元規劃 | 完成 | blocks／snapshot／migration、視窗規劃與驗證、規則 fallback、接縫 |
| P3 導讀子流程 | 程式完成；品質門檻未達 | 子流程、工具、程式驗證、檢查核對；真模型對照已跑，耗時與自動保留率不如 legacy |
| P4 durable 學習流程 | 完成 | events API、命令去重、runner、重啟對帳、預取競爭；21 項測試通過 |
| P5 前端 events API | 完成 | 命令 ID、重送、重載續查、階段顯示、i18n；瀏覽器端到端通過（mock） |
| P6 預取、真模型對照、rollout | 部分完成 | 預取與快取、對照、回退演練完成；人工盲評未做；**未切換預設**（未達驗收） |

## 工作環境與分支

- 開始時 `master` 與 `develop` 指向同一個 commit（`2a216b7`）。工作樹已有使用者未提交的修改：`frontend/src/assets/companions/{fox,otter,owl}.webp`、未追蹤的 `docs/` 與 `frontend/src/assets/companions/STYLE.md`。這些檔案全程不修改、不提交。
- 從 `develop` 建立 `feat/reading-langgraph` 作為整合工作分支（切換不影響上述未提交檔案）。規劃建議每階段一個分支；本次未取得提交授權，所以各階段先在同一工作分支上以未提交變更推進，最後提供建議的 commit 拆分。
- 機器：Apple M4 Pro、24 GB；Python venv 3.13.11（`requires-python >=3.11` 不變）；Ollama 0.35.1，本機已有 `qwen3-vl:2b/4b/8b`；`backend/models/` 已有 `qwen3-1.7b-int4-ov`、`qwen3-8b-int4-ov`（OpenVINO IR）。

## P0 baseline／fixture／評估契約

完成項：

- `backend/evals/reading/manifest.json`：13 份自寫教材、45 個 gold 來源單元，加上 3 個損壞／不支援來源（空檔、壞 PDF、docx）。涵蓋中文、英文、公式＋where、跨頁條件、定義＋例子、清單、程式碼、小標題、重複段落、跨視窗論證、長段落、提前出題、教材內指令文字、無法抽取的圖片。gold labels 以原文片段標註，允許多種切法（`merge_ok`、`allowed_breaks`、`sentence_breaks_ok`）。
- `backend/evals/reading/RUBRIC.md`：人工 1–5 分 rubric、重大錯誤定義與盲評流程。
- `backend/evals/reading_eval.py`：來源完整性、邊界、受保護結構、導讀保留項、forbidden、旁白、出題範圍檢查；盲評包產生與揭盲計分。
- `backend/scripts/eval_reading.py`：`baseline`／`candidate`／`compare` 三種模式，輸出 `results.json`＋`report.md` 到 `backend/data/evals/`（git 忽略）。
- `backend/tests/test_eval_contract.py`：9 項，鎖住評估規則本身。

既有測試基線（改動前）：後端 pytest 36 passed；前端 `bun run typecheck` 通過、`bun test` 22 pass；`agent` typecheck 通過。

既有失敗（不是本次改動造成）：

1. **Ollama 0.35.1 不理會 qwen3-vl 的 `think:false`。** 2b／4b／8b 都把推理文字放在 `thinking`、`content` 為空，JSON schema 也不生效；`/no_think` 無效。legacy adapter 改讀 `thinking`，結果 9/9 段導讀與 13/13 個切段視窗都失敗（「模型輸出不含 JSON 物件」）。README 記載的實測環境是 Ollama 0.34。修正見 P1。
2. **legacy 切段把 code fence 內的 `# 註解` 當成 Markdown 標題**，該行從正文消失（`code-block` 完整性失敗）。
3. legacy 模型切段傾向把整個範圍併成一段（`formula-where`、`cross-page-condition` 各只有 1 段）。

baseline（legacy，qwen3-vl:4b，temperature 0.2，修正 think 問題後）：

| 指標 | 值 |
| --- | --- |
| 總耗時 | 239 秒（13 份教材） |
| 首段導讀 | p50 9.9 秒，最慢 16.5 秒 |
| 每段導讀 | p50 7.1 秒，p95 11.3 秒（27 段） |
| 模型呼叫 | 40 次（切段 13、導讀 27）；1 次 Ollama 500 錯誤 |
| 來源完整性 | 12/13 |
| 邊界符合 gold | 8/13；漏切 8 處 |
| 導讀自動保留檢查 | 89.4%（自動信號，不是人工評分） |

人工盲評尚未進行（需要新流程導讀產出後一起盲評）。

## P1 runtime／LangChain bridge

完成項：

- 依賴：`langchain 1.4.3`、`langchain-core 1.6.6`、`langgraph 1.2.12`、`langgraph-checkpoint-sqlite 3.1.1`、`aiosqlite 0.22.1`（Python 3.11 與 3.13 解析結果相同）。`backend/requirements.lock` 由 `uv pip compile pyproject.toml --extra dev --extra openvino --universal --python-version 3.11` 產生；安裝改用 `uv pip install -e ".[dev]" -c requirements.lock`。
- `app/llm/langchain_bridge.py`：`BackendChatModel` 把 LangChain 訊息轉給既有 adapter。沒有隱藏重試；`bind_tools` 明確拒絕（不宣稱原生 tool calling）；預設強制關閉 LangSmith tracing（`MENTOR_ALLOW_LANGSMITH=1` 才允許）。
- `app/llm/runtime.py`：`ModelSnapshot`（不可變、含 config hash）、`CallBudget`（規劃 §5.5 的上限，可序列化）、`CallRecord` trace、`BackendPool`（同一實體模型共用一個 adapter 與 semaphore）。
- `app/llm/tasks.py`：`run_structured`（LangChain chain → Pydantic → 程式檢查；格式修正每任務最多 1 次，回饋具體錯誤）與 `select_context`（schema 約束的工具選擇；白名單、參數驗證、重複讀取略過、輪數上限）。
- **修正 Ollama adapter**（既有失敗 1）：對 qwen3 系列模型，在訊息最後預填已關閉的 think 區塊 `<think>\n\n</think>\n\n`，Ollama 會接續這則 assistant 訊息；仍是原生 `/api/chat`、`think:false`、JSON schema。legacy 與新流程都受益。

檢查結果：

- `tests/test_llm_bridge.py` 13 項：三種 adapter 經 bridge 的 request 與直接呼叫相同；Ollama `think:false`、schema `format`、`thinking` 退回讀取；OpenVINO `apply_chat_template=False`、`StructuredOutputConfig`、`enable_thinking=False`、`CACHE_DIR`；連線錯誤只呼叫 1 次；預算上限；semaphore 序列化。
- `tests/test_graph_checkpoint.py` 2 項（真的子 process 崩潰＋檔案 SQLite）。
- `scripts/runtime_smoke.py`：Ollama qwen3-vl:4b 兩項通過，3 次呼叫共 3.4 秒（修正前 130 秒且失敗）。OpenVINO qwen3-1.7b-int4（CPU）結構化任務通過、16–22 tok/s；工具選擇的格式合法但選錯單元（模型能力）。

spike 決策（以實際安裝版本驗證）：

- graph 一律以 `durability="sync"` 執行。預設的非同步寫入在硬崩潰時會遺失子流程進度；sync 時子流程在新 process 從中斷節點續跑，前面節點不重跑。
- 同一事件重送 `Command(resume=…)` 會被框架再執行一次，所以去重由 runner 依 `last_applied_event_id` 對帳負責。

限制：OpenVINO B70 GPU、OpenVINO 8b 未驗證。

## P2 來源單元規劃

完成項：

- `app/reading/schemas.py`：`SourceBlock`、`SourceUnit`、`ReadingPart`、`SegmentationPlan`，以及模型回傳的 `WindowPlan`（只有區塊編號與理由）。
- `app/reading/source.py`（`blocks-v1`）：md／txt 依段落、標題、清單項目、code fence（含空行與 `#` 註解）、多行 `$$` 公式、表格、引文、圖片切 block；PDF 頁依段落切，跨頁未完句標 `continues`；過長段落在句尾（不在 `$…$` 內）切成帶 `parent_block_id` 的子區塊；公式／程式碼無安全切點時回報 `range_too_large`。ID 由 snapshot、順序與內容 hash 組成。
- `app/reading/segmentation.py`：程式標示受保護關係（公式與引入／where、同一清單、引入句與其後、跨頁句、圖與說明、標題與內容）與強制邊界（小標題）；規則切法；視窗不切開受保護關係並附唯讀前後文；模型輸出驗證、一次修正、退回規則；以承接語開頭的接縫才請模型判斷（每處最多 2 次）；已完成的視窗與接縫可在重啟後重用；units 轉成 legacy quote／part 形狀，正文由程式從 block 拼接。
- `app/db.py`：`schema_migrations` 與順序 migration（v2：sessions 新增 `workflow_runner`、`revision`、`workflow_meta`；新增 `source_snapshots`、`source_blocks`）；`INSERT INTO sessions` 改為明確欄位。舊資料列 `workflow_runner` 為 NULL＝legacy。
- `app/persistence/repository.py`：snapshot 與 blocks 的保存與讀取（內容定址）。
- prompts：`segment_blocks.md`、`segment_seam.md`。

檢查結果：`tests/test_source_units.py` 20 項；全部後端 80 passed。

真模型切段對照（qwen3-vl:4b，同 manifest）：

| 指標 | legacy | 新切段 |
| --- | --- | --- |
| 來源完整性 | 12/13 | 13/13 |
| 邊界符合 gold | 8/13 | 9/13 |
| 漏切邊界 | 8 | 4 |
| 不可拆內容被切開 | 0 | 3（皆在 `cross-window-argument`） |
| 呼叫／耗時 | 13 次 | 13 次、26.5 秒 |

限制：`cross-window-argument` 的三段論證仍被模型切成多段（接縫規則只在承接語開頭時觸發）；`cross-page-condition`、`list-tcp` 仍有過度合併。legacy session 仍用舊切法，新切法只用於 graph session。


## P3 導讀子流程

完成項：

- `app/reading/agent.py`（契約、程式驗證、放行判斷、檢查意見核對）、`app/reading/tools.py`（5 個唯讀工具與範圍限制）、`app/workflows/reading_graph.py`（analyze → context → draft → validate → review →〔revise → review〕→〔verify〕→ accept／fail）。
- prompts：`reading_analyze`、`reading_context`、`reading_draft`（由 `guide.md` 衍生，教學規則不變）、`reading_review`、`reading_revise`、`reading_verify`。
- migration v3：`reading_artifacts`、`llm_calls`；trace 在每次呼叫後立即寫入。
- 測試：`tests/test_reading_graph.py` 16 項。

真模型除錯後的決策（以 qwen3-vl:4b 實際輸出為依據）：

1. **檢查意見要經程式核對。** 第一次對照時 18 段全部未公開，主因是 4b 檢查模型的誤報：說「漏掉條件」，引用的原文其實已幾乎逐字出現在草稿。現在「漏掉」類意見若引用的原文已大多在草稿中（字元 bigram 覆蓋率 ≥ 0.8），或「寫錯」類意見引用的草稿片段根本不在草稿裡，就降為 `review_unverified` 提醒。出現在「補充說明」段落的內容被判為無依據時，降為 `supplement_flagged`。
2. **跨語言的「漏掉」意見改用摘錄核對**（`verify`，最多 1 次呼叫）：修訂後仍被判漏掉時，請模型從草稿逐字複製涵蓋該內容的句子，程式確認句子真的存在（覆蓋率 ≥ 0.9）才撤銷意見；複製不出來就不公開。
3. 修訂結果漏掉中文課文時，保留前一版的中文課文；修訂 prompt 加上中文課文規則。
4. 上下文工具只在原文開頭有指涉語，或分析結果明確提到前後文時才觸發（4b 幾乎每段都填「需要上下文」）。
5. 檢查步驟輸出上限由 1024 提高到 2048 tokens（評估中 2 段因 JSON 被截斷而失敗）。**這項修正在最後一次對照之後才加入，尚未用真模型重跑。**
- guide 的操作鍵以「單元＋attempt」為單位；品質失敗後重試才開新 attempt。檢查步驟輸出無效時視為未通過（保守）。

真模型對照（qwen3-vl:4b，temperature 0.2，同 manifest；`backend/data/evals/p0-ollama-4b-baseline` 對 `p3-ollama-4b-candidate`）：

| 指標 | legacy | 新流程 |
| --- | --- | --- |
| 來源完整性 | 12/13 | 13/13 |
| 邊界符合 gold | 8/13 | 8/13 |
| 漏切邊界／不可拆內容被切開 | 8／0 | 5／3 |
| 自動保留檢查（must_keep） | 89.4% | 77.7% |
| 缺導讀的 gold 單元 | 6 | 8 |
| forbidden／旁白 | 0／0 | 0／0 |
| 首段導讀 p50 | 9.9 秒 | 36.4 秒 |
| 每段導讀 p50／p95 | 7.1／11.3 秒 | 36.4／73.5 秒 |
| 模型呼叫 | 40（1 次 Ollama 500） | 139（0 錯誤）；超出預算 0 |

新流程 32 段中 26 段公開。6 段未公開：3 段是草稿加了原文沒有的內容（例如「嵴狀結構」、光速快慢比較），依規格應擋；1 段是行內公式沒有原樣保留（程式驗證）；2 段是檢查輸出被截斷（已修，未重跑）。legacy 的 6 個缺導讀來自 `cross-page-condition` 的 Ollama 500 與 `code-block` 的切段完整性失敗。

結論：可追溯性（來源完整性、trace、不公開未通過的草稿）有改善；**品質改善尚未證明**（自動信號較差，人工盲評未做）；**速度為 legacy 的 5–6.5 倍，超過「不超過 3 倍」目標**。

## P4 durable 學習流程

- migration v4：`workflow_commands`、`runner_lease`、`turns.command_id/operation_key`、`sessions.workflow_status`。
- `app/workflows/learning_graph.py`（外層 graph，含 Mermaid）、`runner.py`（單一 worker lease、每 session 串行、啟動恢復、四種對帳情況、預取排程）、`commands.py`（冪等受理與驗證）、`service.py`、`faults.py`（重啟測試用故障點）。
- API：`POST /api/sessions/{sid}/events`、`GET /api/sessions/{sid}/commands/{cid}`、`GET /api/sessions/{sid}/trace`。`MENTOR_WORKFLOW=legacy|langgraph` 只影響新 session；legacy 端點遇到 graph session 回 409 `use_events`。
- 預算鍵：模型呼叫預算以「項目＋命令 ID」計（重試是新 attempt；同一命令重啟後共用），業務提交以「項目」為唯一鍵（不會重複提交）。
- 測試：`tests/test_workflow.py` 21 項。真子 process 崩潰點：ask／next／guide／切段／出題／評分提交後、命令完成標記前。另有重送、同 ID 不同內容、雙 next、ask 與 next 競爭、過期 revision 與單元、guide／ask／出題失敗後重試、提前出題範圍、第二個 runner 被拒、graph session 的匯出與重跑、預取採用／失效／被取消。

## P5 前端

- `frontend/src/workflow.ts`：每個操作送出前產生 command ID 並存入 sessionStorage；沒有回應時用同一 ID 重送（最多 3 次）；輪詢 `GET /commands/{id}` 到完成；重新載入時先查狀態，伺服器沒收到（404）才重送。
- `ReadingPanel.tsx`、`Learn.tsx`：graph session 改送命令（next／finish／ask／answer／retry），舊 session 維持原 API。發問或作答失敗時保留輸入，重試以 `retry` 指向失敗的命令。阻塞錯誤（導讀、出題等）顯示可重試狀態。`Working.tsx` 顯示「正在整理這一段要教的重點／對照原文檢查導讀／修正導讀」等學生看得懂的階段。
- i18n：新增 5 個字串，英文手寫並同步 `en.source.json`（沒有執行會呼叫雲端翻譯的腳本）。
- 匯出：Markdown 加上流程版本、prompt bundle hash、模型設定 hash，以及每段導讀的檢查結果。
- 測試：前端 `bun test` 30 pass（新增 `ReadingPanel.graph.test.tsx` 8 項）、`bun run typecheck` 通過。
- 瀏覽器端到端（headless Chrome，mock 模型，隔離資料目錄，不碰 `backend/data/`）：導讀 → 發問 → 下一段後立刻重新整理（重載後查到命令，未重送）→ 出題 → 作答 → 學習小結。結束時 `next` 恰好 3 次、提問 1 筆、turns 2，沒有重複。Claude in Chrome 擴充功能未連線，所以改用 Chrome DevTools 協定驅動。

## P6 預取、對照、rollout

- 預取：學生停在第 k 段時，runner 在背景以「第 k 段已讀」的輸入產生第 k+1 段導讀，存成 `guide_prefetch` 快取（鍵含來源、前文重點、問答、prompt、模型設定、workflow 版本）。按「下一段」時鍵相符就直接採用，不呼叫模型；學生發問會改變鍵。新命令進來時取消進行中的預取；已開始的本機推論無法搶占。`MENTOR_READING_PREFETCH=0` 可關閉。**預取尚未用真模型量測實際首段等待時間。**
- 回退演練（隔離環境）：以 `MENTOR_WORKFLOW=legacy` 重啟正式 uvicorn 程序後，既有 graph session 的命令照常完成，新 session 走 legacy；同時驗證重啟後續接。
- 盲評包：`backend/data/evals/p6-compare-4b/blind/packet.md`（31 題）。**尚未人工評分。**
- **未切換預設**：品質未證明改善、速度未達標，`MENTOR_WORKFLOW` 預設維持 `legacy`。

## 已知限制

- 新流程真模型速度為 legacy 的 5–6.5 倍；預取可能縮短學生感受到的等待，但未量測。
- 人工盲評未完成；不能宣稱導讀品質提升。
- `cross-window-argument` 的論證仍會被切成多段。
- OpenVINO 只在 CPU（qwen3-1.7b）驗證過 bridge；B70 GPU、8b OpenVINO 未驗證。
- 檢查輸出上限的修正尚未用真模型重跑。
- 首版只允許一個 runner；多 worker 需另外設計 lease heartbeat。
- 舊版程式不能直接接手升級過的 DB（見 README 回退說明）。

## 下一輪閱讀方法落實

2026-10-04 對照 WaAi-ML 規範後，確認既有實作仍缺少來源先驗證、正文獨立覆蓋、完整 token 預算與壓縮後忠實性檢查。審查重跑後端結果為 **117 passed，5 個 PyMuPDF／SWIG 棄用警告**；沒有重跑真模型。

已用程式與 scripted backend 重現：正文缺少條件與公式，但中文課文保留時，graph 仍可能接受；反轉獨立性前提的句子因高 bigram 覆蓋使審稿意見降級；不同第三層小節可能落在同一 Part。另確認空 review 物件會被 schema 解析為空 issues，尚未確認真模型曾產生此行為。

完整證據、契約與驗收見 [閱讀方法落實規劃](reading-method-alignment-plan.md)。先修放行漏洞與交接預算，再建立來源與教學契約；不再以直接合併 analyze／draft 或省略 review 作為首要優化。後續若調整呼叫分工，須證明品質關卡仍成立。

| 階段 | 狀態 | 摘要 |
| --- | --- | --- |
| R0 固定現況與回歸案例 | 完成 | 5 個放行漏洞＋1 個結構案例成為 fixtures；修改前 graph 與 legacy 的真模型基線另存 |
| R1 正文驗收與審稿降級 | 完成（併入 R3 的 Gate 2） | 中文課文與正文分開核對；相似度與「補充說明」標籤不再撤銷重大問題；空的審稿不算通過 |
| R2 上下文與輸入預算 | 完成 | 每次呼叫的輸入＋輸出保留＋餘量檢查；ContextPacket 只捨棄可選上下文；工具回傳實際來源與完整性 |
| R3 來源與必要教學契約 | 程式完成；審查後補修 Gate 1 改寫命題與橋梁核對；真模型部分通過 | 逐句命題＋Gate 1、BridgeSpec、學習狀態、Gate 2、VerifiedSource 快取與失效規則 |
| R4 原書結構與模型批次 | 完成 | heading level、section path、論證／例子／列舉關係、一個 Part 最多一個第三層小節 |
| R5 PDF 符號與媒材證據 | 程式完成；審查後補上取證公式的覆蓋檢查；qwen3-vl:4b 視覺取證在自製 fixture 通過 | 頁／bbox／字型旗標／遺失字形、區域渲染、能力宣告、未確認時不公開 |
| R6 筆記與題目忠實性 | 程式完成；未用真模型跑完整出題 | 筆記只從已驗證命題壓縮並逐條核對；題目依主題計畫與已驗證依據出題並核對 |
| R7 相容性與前端整合 | 完成 | 舊版 DB／checkpoint（38e3c11 實際產生）升級與續接、匯出、前端狀態與 i18n |
| R8 真模型品質與效率 | 自動對照完成；盲評包已依執行識別重產；人工盲評未做 | 見下方 R8；未達切換預設的條件 |

### 工作環境、分支與版本

- 本輪從 `feat/reading-langgraph`（含 P0–P6 commit）建立 `feat/reading-method-alignment`。`develop` 仍停在 `2a216b7`，沒有 P0–P6 的基礎，所以沒有切回 `develop` 重做。使用者原有的未提交檔案（companion 圖檔、`STYLE.md`、`docs/` 下的規劃與交接文件）全程未修改、未提交。
- 環境：Apple M4 Pro、24 GB；Python 3.13.11 venv；Ollama 0.35.1（本輪在本機啟動 `ollama serve`，沒有下載任何模型），使用已存在的 `qwen3-vl:4b`；溫度 0.2。OpenVINO 沒有在本輪重跑（見限制）。
- 開工時重跑後端測試：**117 passed，5 個 PyMuPDF／SWIG 棄用警告**，與審查基線相同。
- WaAi-ML 方法來源（唯讀）讀取版本：`git 6ee8af1`；`ReadingAgent.md` sha256 `33200a5b…`、`reading-guide-format.md` `ec262c0e…`、`reading-guide-section-format.md` `0b58eb15…`、`notation-governance.md` `ad12e2be…`。已核對 ReadingAgent §2、§3、§5.2、§6、§7、§8，format 的 §2、§4、§5，notation 的 §5、§6，以及 `reading-plain_HoML-CH1-2.md` 的細讀／略讀用法。書籍專屬 Agent（HoML／AIMA 等）的表記慣例沒有逐條落實（本專案的教材不限定一本書）。
- 新版本識別：`reading-graph-v2`、`reading-bundle-v2`、`reading-contracts-v1`、`blocks-v2`、`units-v2`、`notes-v1`、`pdf-evidence-v1`、`markers-v1`、token 估計 `heuristic-v2`；DB migration v5。

### R0 固定現況與回歸案例

- `backend/evals/reading/regressions/cases.json`：審查重現的 4 個放行漏洞（正文漏條件與公式但中文課文保留、反轉「彼此獨立」、空審稿、錯誤內容標成補充說明）、一個應通過的忠實改寫（避免靠全部拒絕達標），以及兩個第三層小節落在同一 Part 的結構案例。模型輸出依角色寫在 fixture 中，同一組資料可跑不同版本的 graph。
- 在修改前的 commit（`38e3c11`，獨立 worktree）上執行：5 個漏洞全部重現（4 個草稿被公開、兩個小節併成 1 個 Part、`ReviewResult({})` 解析成空 issues），忠實改寫正常公開。紀錄：`backend/data/evals/r0-regressions-prechange.jsonl`。
- 修改前的真模型基線（qwen3-vl:4b，同 manifest）：`backend/data/evals/r0-graph-prechange`、`r0-legacy`（git 忽略，未覆寫 P0–P6 的舊結果）。

| 指標 | legacy（r0） | 修改前 graph（r0） |
| --- | --- | --- |
| 公開／導讀數 | 27／28 | 23／32 |
| 每段 p50／p95 | 9.4／15.6 秒 | 34.7／76.9 秒 |
| 首段 p50 | 13.1 秒 | 49.7 秒 |
| 模型呼叫 | 41 | 143 |
| 輸入／輸出 tokens | 42,549／11,234 | 160,694／54,071 |
| 來源完整性 | 12/13 | 13/13 |
| 自動保留率（正文＋課文） | 0.926 | 0.649 |

修改前 graph 的 9 段未公開：7 段 `quality_check_failed`（多數為審稿指出無依據內容）、2 段 `invalid_model_output`。

### R1 正文驗收與審稿降級

- `reading/agent.py`：程式檢查逐欄位進行；正文的公式只在正文裡找。正文完全沒有數學時，漏掉的公式是確定的事實（直接修訂）；正文有數學但不是逐字相同時，記為「待確認」，要由核對提出正文中改寫後的公式才可撤回（等價改寫不能由撰寫者自我宣告）。
- 移除 bigram 覆蓋降級與「補充說明」降級。審稿意見的引用只在原文與「該意見所指欄位」中查找；引用不存在、沒有引用時記為「待確認」並經一次聚焦核對。撤回重大意見必須：核對給出的正文原句確實存在、句中保留來源的否定／條件／例外／量詞／必要性（`reading/markers.py`）、保留來源的公式。引用有效的「無依據」或「改變意思」意見只能靠修訂處理。
- 審稿 schema 要求每個檢查項都回報一次（缺少、重複、空物件都會先要求格式修正，仍不完整就 `review_invalid`，不公開）。這套規則在 R3 成為 Gate 2 的一部分。
- 測試：`tests/test_reading_regressions.py`（fixtures 全部依預期公開或擋下，且擋下的原因正確：程式公式檢查、否定反轉、檢查結果不完整、未核准的補充說明）。

### R2 上下文與輸入預算

- `app/llm/context.py`：`estimated_input + reserved_output + margin ≤ effective_limit` 在 `run_structured` 的每一次送出前檢查（含格式修正；放不下時，格式修正只附錯誤說明、不附無效輸出）。放不下就 `context_overflow`，不呼叫模型。
- 有效長度來自 adapter 的能力宣告（`Capabilities`）：Ollama 由 `/api/show` 取得模型的 context length 與是否支援 vision；OpenVINO 讀 IR 的 `max_position_embeddings`，並明確標示 `context_tokens` 對它沒有作用；再以 `MENTOR_MAX_CONTEXT_TOKENS`（預設 32768）為上限。Ollama 的 `num_ctx` 改用固定級距（`MENTOR_GRAPH_NUM_CTX`，預設 16384），不再每個任務換一次而觸發模型重載。
- `Packet`／`ContextItem`：必要項（原文、命題、橋梁、理解所依賴的補充上下文、被核對的正文）永不截斷；可選項（已讀重點、以前的問答、讀者已讀命題）依優先序整項捨棄並留下「未提供」標記；每次呼叫的 packet 與預算寫入 `llm_calls.request`。答疑也改用 packet。
- 工具：`ToolPayload` 回傳實際讀到的單元、版本、是否完整與未提供項目；不再按字元截斷，單元整段放入或明確列為未提供。`source_refs` 記錄實際讀到的單元（讀 u-b 的前一段會記 u-a）。撰寫、核對、修訂、聚焦核對使用同一份補充上下文。
- 估計方法 `heuristic-v2`（CJK 1、其他字元 0.35、每則訊息 8、基本 24、每張圖 1280）；OpenVINO 載入後可用 tokenizer 精算。真模型的估計／實際比見 R8。
- 測試：`tests/test_context_budget.py`。

### R3 來源與必要教學契約

- 契約：`reading/contracts.py`（TaskContract、LearningSnapshot、SourceClaim、SourceUnderstanding、GateResult、VerifiedSource、BridgeSpec、BodyDraft、BodyAudit）。程式與 Gate 邏輯：`reading/gates.py`。子流程：`workflows/reading_graph.py`（`reading-graph-v2`）。
- 階段 A／Gate 1：程式先把原文逐句編號，模型的命題只能引用句子編號（只涵蓋一句的一部分時才逐字引用）；外文逐句翻譯，中文課文由程式組裝。程式核對：每句都被引用、部分引用時涵蓋 ≥85% 且每個條件／否定／例外／量詞／必要性標記都在引用範圍內、命題陳述保留原句的這些標記、外文譯句保留公式與標記。外文另由模型逐句核對譯文；中文原文只有在標記可疑時才呼叫核對。符號：md／txt 以作者的 LaTeX 為證據；PDF 見 R5。Gate 1 沒過不會寫任何正文。
- 真模型除錯後的決定：第一版要求模型逐字複製原文，qwen3-vl:4b 的輸出被截斷；改為引用句子編號後仍會不停追加重複的組合命題直到上限（61 秒、4096 tokens 截斷）。最後以受限解碼 schema 限制：句子編號為 enum、命題數上限約為句數＋3、外文譯句數必須等於句數。同一段降到 567 tokens、9 秒。稽核類輸出也用「必須逐一回報這些編號」的 schema（enum＋固定項數），並附上已填好編號的輸出骨架。
- 階段 D：`plan_bridges` 依已驗證命題與讀者狀態規劃 0–4 個橋梁；每個橋梁必須指向存在的命題、說明依據類型與邊界、內容不可描述產製過程。障礙與理由只在內部，草稿 prompt 只看到核准的學科內容。
- 學習狀態：已讀（按下一段）的單元命題、在已讀單元問過的問題、作答證據（目前在閱讀階段為空）、任務契約（深度、目標、讀者背景，未提供時記為預設）。已生成或預取但未讀的導讀不計入；`presented` 與 `read` 分開記錄。
- 階段 E／Gate 2：正文只看已驗證命題、核准橋梁與（可選的）中文課文。程式檢查正文本身的公式、KaTeX、依賴中文課文的說法、叫學生回看原文、敘述產製過程；「補充說明」段落必須對應某個核准橋梁的內容（只用重疊度提出問題，不用來撤回問題）。模型稽核只看正文與原文全文，對每個命題與橋梁逐一回報；「已涵蓋」必須附正文片段，程式展開成整句後核對標記與公式；還要列出命題清單以外但正文漏掉的原文。一次修訂、每個正文版本最多一次聚焦核對。
- JSON 的 LaTeX 反斜線：真模型有時以單一反斜線寫 `\frac`、`\bar`、`\top`、`\nabla`，嚴格 JSON 會把它們解成換頁、退格、tab、換行，公式被悄悄破壞。現在解析前修復 JSON 字串內的 LaTeX 反斜線（真正的 `\n` 換行保留）。這個問題 legacy 流程也有，本輪沒有修改 legacy。
- 持久化與失效：VerifiedSource 以「原文與區塊、補充上下文、符號證據、來源階段 prompts、模型、版本」為鍵存成 `verified_source` artifact；補充上下文全為原文單元時可跨 session 重用。橋梁與正文依讀者狀態，不共用；預取鍵含學習狀態雜湊。改來源、符號證據或來源 prompt 會重驗來源；只改正文 prompt 或讀者狀態不會。
- 預算：每段最多 14 次呼叫（含 2 次格式修正、來源與正文各 1 次修訂），每個呼叫點最多 1 次格式修正，重啟由 `llm_calls` 重建，不會重置。
- 測試：`tests/test_reading_graph.py`（27 項）、`tests/test_learning_state.py`、`tests/test_reading_regressions.py`。

### R4 原書結構與模型批次

- `SourceBlock` 新增 `heading_level`、`structure_confidence`、`meta`（migration v5）。Markdown 標題深度 ≤3 構成正式小節路徑（`#`→章、`##`→節、`###`→第三層小節；此對應寫在 `segmentation.SECTION_LEVEL`），正文繼承所屬小節；PDF 以版面猜到的短標題記為低信心，不定義小節。
- 保護關係：既有的硬性關係之外，新增「論證結論承接前提」「同一組列舉」「例子與其說明的內容」「程式碼與說明」「表格與說明」「同一段落被切成多塊後的後續句子」等軟性關係；合併後超過 1600 字就分成相鄰單元，記錄關係，並保證在同一 Part、後一單元以前一單元作為程式提供的上下文。視窗（模型批次）不切在任何關係中間；模型切法違反關係時被拒絕。
- Part：一個 Part 最多一個正式第三層小節；同一小節內約 5000 字分一次，但不會切在相依單元之前。Part 契約記錄 section path、分割理由與結構信心。接縫檢查在承接語或指涉語開頭時才請模型判斷。
- 測試：`tests/test_structure.py`（結構 regression、深層標題、PDF 低信心標題、`cross-window-argument` 符合 gold 且視窗不切開、例子／程式碼／where、超長論證、模型切法被拒、blocks-v1 舊資料 migration）。

### R5 PDF 符號與媒材證據

- `reading/pdf_evidence.py`：匯入範圍時從原始 PDF 取每個段落的頁碼、bbox、text spans（字型、大小、旗標）、粗體／斜體單字母、上標、數學字型、文字層遺失的字形，以及鄰近圖片與 caption。`materials.pdf_page_paragraphs` 讓匯入與取證使用同一份段落切法。
- 含符號的 PDF 段落（或有字型線索）：session 的模型若在能力宣告中支援影像（Ollama `/api/show` 的 `vision`），只渲染那一段的版面區域送給同一個模型判讀，並與字型線索交叉比對（粗體、斜體、上標）；一致才記為 `confirmed`，判讀結果作為來源理解的符號依據，Gate 1 再檢查命題與中文課文有沒有把粗體向量寫成一般字母。模型不能看圖、沒有原始 PDF 或判讀矛盾時，標為 `notation_unverified` 並在任何模型呼叫前停下（原因含頁碼與可採取的做法），不從文字層猜。渲染圖存在 `backend/data/evidence/`。OpenVINO adapter 宣告不支援影像，送圖會明確報錯。
- 自製 fixtures：`backend/evals/reading/pdf/`（`make_fixtures.py` 產生 `symbols.pdf`、`figure.pdf`）。文字層把 `w ← w − η∇L` 抽成 `w�·�w�·�··L`、把 x² 抽成 `x2`。
- 真模型：`scripts/vision_probe.py --model qwen3-vl:4b --runs 2` 兩次都讀出 `\mathbf{w} \leftarrow \mathbf{w} - \eta \nabla L`，粗體 w、斜體 x／L、上標 2 與字型線索一致（每次 11–14 秒）。這只證明本機 4b 在這個自製 fixture 上可用，不代表真實教科書頁面的準確率。
- 掃描 PDF 仍沿用既有的「沒有可擷取的文字」錯誤，沒有 OCR。
- 測試：`tests/test_pdf_evidence.py`、`tests/test_reading_graph.py` 的 PDF 項目。

### R6 筆記與題目

- `reading/notes.py`、`learning_graph.compact`／`generate_quiz`。筆記只從 Part 內已通過 Gate 1 的命題壓縮，每條標明依據（`U2.C3`），程式核對引用存在、壓縮後沒有掉條件／否定／例外／量詞、公式保留，再由模型逐條核對；兩者都過才是 `verified`。沒有被已驗證筆記涵蓋的已驗證命題原樣保留。讀者看到的筆記（本節重點、易混淆、術語）由程式從已驗證筆記組成，不再有 300–600 字的自由摘要。筆記無法產生時保留已驗證命題並記錄原因，不擋住閱讀；只有模型連不上時才停下等待重試。
- 出題：依據只取已讀範圍內的已驗證筆記與未被涵蓋的已驗證命題；導讀沒過的段落不提供依據。程式排出主題（已讀 Part，有提問的優先，最多 3 個），每個主題一個概念，概念與每題都要標依據；再由模型逐題核對（可由依據回答、條件適用、不需範圍外知識、提示不洩答案），沒過的概念移除，全部沒過就不出題。沒有任何已驗證依據時回 `quiz_basis_insufficient`，不硬出題。小節統整檢查跨 Part 的符號一致性（同一字母有粗體與一般寫法時提醒）。
- 評分與教學決策仍由 `engine.py` 負責，沒有另做評分引擎。
- 測試：`tests/test_notes_quiz.py`。

### R7 相容性與前端整合

- 舊資料：`tests/fixtures/v1_session/` 是用修改前的程式（`38e3c11`）實際產生的 DB 與 checkpoint（一個 session 停在等待讀者、問過一個問題；一個已在作答階段）。新程式開啟後完成 migration v5、載入舊 checkpoint；等待中的 session 在下一個命令前升級一次（`workflows/migrations.py`）：保留它自己的 ask／assess／切段提示詞，加入 v2 提示詞，補上任務契約（記為預設），已公開的 v1 導讀保持原樣並標明舊版檢查，之後的段落走 v2；用 v1 讀過的段落出題時只以原文為依據。作答中的 session 照常作答。legacy session 不受影響。升級紀錄寫在 `workflow_meta.migrations` 與匯出。
- 讀書工作區（Markdown 鏡像）遺失時寫入不再讓命令失敗。
- 匯出：每段導讀標明檢查版本與 Gate 1／Gate 2 結果、閱讀設定、流程升級紀錄、已核對筆記、題目依據。API：`POST /api/sessions` 新增 `reading_depth`、`reading_goal`、`reader_background`；workflow view 新增 `reading_workflow_version`、`task_contract`、`migrations`；session view 的概念與題目附 `basis`。
- 前端：新增 13 個階段文字（例如「正在核對這一段原文說了什麼」「正在檢查導讀是否完整忠實」）；導讀或出題被擋下時顯示白話原因，檢查紀錄（含命題編號）收在可展開區；不能靠重試解決的情況（例如段落太長）不顯示重試，改說明可行做法。i18n 英文手寫並同步 `en.source.json`（沒有執行會呼叫雲端翻譯的腳本）。
- 測試：`tests/test_compat.py`；前端 `ReadingPanel.checks.test.tsx`。

### R8 真模型品質與效率

條件：qwen3-vl:4b（Ollama 0.35.1，本機已有權重）、溫度 0.2、同一份 manifest（新增 `pdf-symbols` 共 14 份可讀教材＋3 份損壞來源）、同一台 M4 Pro。所有輸出在 `backend/data/evals/`（git 忽略）：`r8-legacy`、`r0-graph-prechange`、`r8-graph`（中途版）、`r8-graph-final2`（最終程式）、`r8-graph-rep2`（困難案例重跑）、`r8-report.md`（`scripts/r8_report.py` 產生）。

| 指標 | legacy | 修改前 graph | 修改後 graph（最終） |
| --- | --- | --- | --- |
| 公開／導讀數 | 28／29 | 23／32 | 23／35（另一次同程式的完整重跑 26／35） |
| 每段 p50／p95 | 8.9／14.0 秒 | 34.7／76.9 秒 | 31.3／100.6 秒 |
| 首段 p50 | 11.5 秒 | 49.7 秒 | 31.4 秒 |
| 導讀總時間 | 266 秒 | 1,311 秒 | 1,422 秒 |
| 模型呼叫 | 45 | 143 | 198 |
| 輸入／輸出 tokens | 46,401／11,398 | 160,694／54,071 | 243,197／58,948 |
| 來源完整性 | 13 | 13 | 14 |
| 邊界符合 gold | 8 | 8 | 11 |
| 自動保留率（正文＋課文／只看正文） | 0.845／0.845 | 0.649／— | 0.649／0.649 |
| forbidden（含注入） | 0 | 0 | 0 |

- 未公開原因（最終）：`quality_check_failed` 9、`notation_unverified` 2、`invalid_model_output` 1。逐段檢視後：
  - 確實的問題：正文把 `\mathbf` 改成 `\bm`／`\boldsymbol`（符號改變）；「補充說明」加入沒有核准的背景（包括在只講文字的段落補上梯度下降公式）；「只描述」被改寫而失去「只」；把可能性（may／can）寫成必然；正文改變命題（粒線體段落）。
  - 檢查本身的限制：稽核模型把兩句合併後再引用，程式找不到原句（`image-limitation`、`duplicate-paragraphs`），這些可能是正確稿被擋。
  - 證據限制：`cross-page-condition` 是沒有原始 PDF 的頁面 fixture，符號無從取證（設計上不公開）；`pdf-symbols` 第一段在這一輪的視覺判讀漏寫上標，被交叉比對擋下（重跑那一輪兩段都通過）。
  - 模型故障：`early-quiz` 第一段兩次都陷入重複輸出 `\n` 直到 2048 tokens 上限。
- 各節點時間（最終）：`body_audit` 398 秒、`draft` 386 秒、`understand` 207 秒、`plan_bridges` 176 秒、`revise_body` 148 秒、`verify` 88 秒、切段 35 秒、`source_audit` 9 秒、`symbols` 8 秒。時間主要花在正文撰寫與正文稽核兩步。
- 速度：p50 約為 legacy 的 3.5 倍、p95 約 7.2 倍，**未達「p95 不超過 3 倍」**。修改前 graph 的 p95 為 5.5 倍；本輪多了來源核對與橋梁規劃，換來的是可核對的命題、逐項正文核對與較少被截斷的輸出。
- 預取（`scripts/prefetch_probe.py`，假設每段閱讀 20 秒）：改動前每次預取都在讀者按下一段時被取消並重做（`r8-prefetch.json`：3 次取消、0 次採用）。改為「下一段」等待進行中的預取並直接採用後，`cross-window-argument` 第一次按下一段只等 1.5 秒（不預取時 31.3 秒），4 段中採用 2 段；另一段的預取遇到 Ollama 500 錯誤而改為按需產生（等 47 秒）。背景成本：4 段多出 15 次呼叫。其他兩組測試在第一段或第二段就因導讀被擋而停在重試，樣本太小，不足以下結論。
- Token 估計（`scripts/token_calibration.py` 以 `num_predict=1` 取得 Ollama 的實際 prompt token 數）：61 個實際 prompt 形狀的估計／實際比 1.02–1.36（從不低估）。真模型輸出進入 prompt 時最低到 0.78（格式修正夾帶了重複 `\n` 的無效輸出）；為此把餘量提高到 10%＋256，且格式修正放不下時只附錯誤說明。
- 盲評包：`data/evals/r8-compare-legacy/blind/packet.md`（legacy 對最終 graph，25 題）、`r8-compare-graph/blind/packet.md`（修改前對修改後，21 題）。**尚未人工評分**；mock 或 LLM 自評都沒有拿來代替人工分數。
- 錯誤稿漏放／正確稿誤擋：在 regression fixtures（scripted）上，4 份錯誤稿全擋、1 份正確改寫通過。真模型的實際比例需要人工盲評才能判定；上面列出的「稽核合併引用」屬於可能的誤擋。
- OpenVINO（CPU，`qwen3-1.7b-int4-ov`，`zh-opportunity-cost`；`r8-openvino-1.7b-cpu`）：adapter 路徑可用。受限 schema 在 OpenVINO structured output 下都產生合法輸出；pipeline 載入後 prompt token 改用 tokenizer 精算，估計等於實際（第一次呼叫尚未載入，用估計 1106 對實際 911）。但 1.7b 只為第一句列出命題，Gate 1 以「S2 沒有任何命題涵蓋」擋下 2/2 段，切段視窗也無效而退回規則切法。結論：1.7b 不足以完成這套來源核對，關卡拒絕公開而不是放行不完整的理解。8b OpenVINO 與 GPU 未測。

結論：可追溯性、來源核對與符號取證都有可驗證的進展；**品質是否提升尚未經人工證明，速度未達標**，`MENTOR_WORKFLOW` 預設維持 `legacy`，不宣稱 rollout 可用。

### 審查後修正（2026-10-04）

使用者核對程式後以小型反例重現 5 個缺口。狀態：完成一輪實作與自動測試，仍有品質關卡缺口，尚未完成驗收；預設維持 `legacy`。以下修正都有對應反例測試，**沒有用真模型重跑**，所以上方 R8 的真模型數字仍是修正前的程式。

1. **Gate 1 改寫命題**：引用句子只證明來源位置，不證明理解正確。命題改寫了陳述，或自行填了條件、例外、力度、原文沒有的公式時，記為 `rewrite_check`，必須經語意審稿判為忠實才能通過並寫入 VerifiedSource 快取；直接沿用原文的命題仍走快速路徑。反例：「矩形面積等於長乘以寬」寫成「長加上寬」，審稿判為改變原意時擋下且不寫入快取。
2. **JSON 解析**：合法 JSON 一律保留原值。只有嚴格解析失敗，或解析結果出現 LaTeX 被吃成控制字元的明確痕跡（退格、換頁，或公式中 tab／換行接著 `op`、`abla` 等指令殘字）時，才改用 LaTeX 反斜線修復。自我跳脫換行的修正改為只作用在呼叫端指定的散文欄位（正文、筆記），只處理沒有真正換行的字串，並跳過程式碼區塊與行內程式碼。反例：`print("\\n")`、正規表示式與逐字引用都保持原樣。
3. **PDF 取證公式**：視覺取證得到的每個公式都是必要覆蓋項目。Gate 1 要求命題寫出這些公式（`symbol_formula`）；正文撰寫、修訂與稽核的 prompt 帶入公式與取證區域；Gate 2 由程式檢查正文是否寫出。反例：命題或正文完全沒有公式時都會被擋下。
4. **教學橋梁**：規劃核准不等於內容正確。稽核標 `ok` 時必須附正文片段，程式展開成整句並確認與橋梁內容相符；內容正確性另外回報（`correct`），依據原文的橋梁必須引用存在的原文句子編號，推導與先備知識要寫出理由。無法確認時記為待確認，經聚焦核對才能撤回；由模型再核對通過的會標記 `bridge_checked_by_model`，因為仍是同一個小模型的判斷。
5. **盲評包**：改用執行識別（結果目錄名稱）作為 key，兩輪 candidate 不再合併計分；新增「未公開草稿審查」區段與 `score_held`，用來計算正確稿被誤擋的數量（舊結果檔沒有保存草稿時，從 trace 取回最後一次正文）。已重新產生：`r8-compare-legacy`（25 組配對＋9 份未公開草稿）、`r8-compare-graph`（21 組配對＋9 份未公開草稿）。修改前 graph 的結果檔沒有保存原始輸出，它被擋下的草稿無法取回；最終輪另有 3 段沒有可審的草稿（2 段在寫正文前因符號證據停下、1 段輸出無效）。

測試：後端 `tests/` 全部通過（含上述反例），前端未改動。

評估資料事件：重產盲評包時，我用 shell 變數組出的 `rm -rf` 路徑展開為空值，誤刪整個 `backend/data/evals/`。使用者已從本機 Time Machine 快照（11:18）還原；已核對 P0–P6、R0、R3 smoke、R8 各輪與量測檔都在，且 R8 報告數字與先前回報一致。

### r9 真模型重跑、遠端推論與錯誤分類（2026-10-04）

程式版本：r9 以 commit `1726849` 執行（含上方 5 項審查後修正、稽核合併引用的逐段核對、逐字寫出的命題由程式確認涵蓋）；不含之後才加入的正文 `maxLength` schema。

真模型重跑（本機 M4 Pro，`qwen3-vl:4b`，循序；`data/evals/r9-report.md`）：

| 指標 | legacy | 修改前 graph | final2 | r9 |
| --- | --- | --- | --- | --- |
| 公開 | 28/29 | 23/32 | 23/35 | 24/34 |
| 每段 p50／p95 | 8.9／14.0 秒 | 34.7／76.9 秒 | 31.3／100.6 秒 | 30.3／84.3 秒 |
| 模型呼叫 | 45 | 143 | 198 | 211 |
| 邊界符合 gold | 8 | 8 | 11 | 10 |
| 自動保留率 | 0.845 | 0.649 | 0.649 | 0.66 |
| 未公開原因 | 模型無法使用 ×1 | 輸出無效 ×2、品質關卡 ×7 | 品質關卡 ×9、符號未確認 ×2、輸出無效 ×1 | 品質關卡 ×6、輸出無效 ×2、符號未確認 ×2 |

- 品質關卡擋下的段落由 9 降為 6；`source_audit` 由 5 次增為 23 次，這是改寫命題必須語意核對的成本。速度仍未達標（p95 約 legacy 的 6 倍）。
- 重複測試（`r9-graph-rep`，4 份教材）：公開 6/9（r8 重複測試 8/10），自動保留率 0.536（0.643）。p95 241.6 秒來自 `formula-where` 第 2 段：兩次 `verify` 各 63 秒與 40 秒、`body_audit` 共 65 秒。每份教材只跑一兩輪，這個差異可能只是隨機變化，需要多輪才能判斷。
- 預取（`r9-prefetch-cross-window-argument.json`，假設每段閱讀 20 秒）：等待 p50／p95 由 32.7／50.2 秒降為 29.6／30.2 秒；第一次翻頁 0.2 秒；採用 3 段；背景多 20 次呼叫。後續段落仍要等約 30 秒，因為一段導讀生成 30–50 秒，比閱讀時間長。
- 正文 `maxLength`：Ollama 會遵守（上限 50 字時輸出正好 50 字），所以正文長度可以在生成時就受限制。
- 完整 session（`r9-sessions/`，真實 app 讀完教材後產生筆記與題目）：`headings` 4 段全公開、6 條筆記全核對通過、6 題都有依據；`early-quiz` 18 條筆記擋下 3 條（例如原文「紫光偏折最多、紅光偏折最少」被寫成「紫光折射率大於紅光」，程式與核對都指出），未涵蓋命題 `U1.C2` 有記錄；`formula-where` 第 1 段重試後仍被擋，提前結束並只用已讀內容出 2 題。人工抽查表在 `r9-sessions/spotcheck.md`，**尚未人工檢查**。
- 盲評包：`r9-compare-legacy`（legacy 對 r9，28 組配對＋6 份未公開草稿）、`r9-compare-graph`（修改前 graph 對 r9，22 組配對＋6 份未公開草稿）。r8 的盲評包保留。**尚未人工評分**。

遠端推論（品質驗收用）：

- Ollama adapter 新增：token 驗證標頭（`MENTOR_OLLAMA_TOKEN`，只從環境變數讀取）、外送防護（主機是公開 IP 或公開網域時，必須設定 `MENTOR_ALLOW_REMOTE_MODEL=1`；本機、區網與 Docker 服務名稱不受影響）、可選 seed（`MENTOR_OLLAMA_SEED`）、模型 digest 查詢。
- 評估腳本記錄 `provenance`（主機、遠端與本機 digest、seed、平行數）並分類：`local-acceptance`、`local-parallel`、`remote-quality`（digest 與本機相同，品質可驗收）、`remote-unverified`（不可驗收）、`mock`。本機 Ollama 未啟動時，以本機 manifest 檔的 SHA-256 比對（已確認等於 `/api/tags` 的 digest）。耗時只認本機循序執行。新增 `--jobs N` 平行跑多份教材；mock 下平行與循序結果一致。
- `backend/scripts/colab/`：token 驗證 proxy（只轉送 4 個端點，模型生成期間送空白避免 tunnel 約 100 秒逾時）與「全部執行」即可完成設定的 notebook。第一版 notebook 讀到 tunnel 網址後不再讀取 `cloudflared` 輸出，pipe 塞滿後 tunnel 卡住並回 530；已改為寫入紀錄檔。
- 校準（`r9-remote-a100`，Colab A100，6 份平行，digest 與本機相同）：公開 24/34、邊界 10、來源完整性 14、品質關卡 6、符號未確認 2，都和本機 r9 相同；自動保留率 0.68（本機 0.66）。整輪 421 秒（本機 1392 秒）。結論：遠端可用來驗證 prompt 與流程的品質；比較修改前後時，同一份教材應多跑幾輪看分佈。

錯誤分類修正：Ollama 因模型重複輸出而中止生成（`prediction aborted, token repeat limit reached`）原本被歸為「模型無法使用」。現在 adapter 丟出 `DegenerateOutput`（`LLMError` 的子類別，舊版流程行為不變），`run_structured` 把它當成輸出無效，以一次附上「不要重複」提醒的重試處理，仍失敗才記為 `invalid_output`。遠端校準中的 2 次 `model_unavailable` 就是這種情況。

### 第二次審查後修正（2026-10-04）

使用者複查時以反例重現 3 個 P1 問題。三項都先寫成回歸測試，並確認測試在修正前的程式（`76305c6`）上失敗、修正後通過。**這次修正沒有用真模型重跑**，所以上方 r9 數字仍是修正前的程式。

1. **舊快取繞過新的關卡**：驗證規則改了，但 VerifiedSource 的快取鍵沒有變，修正前寫入的「矩形面積＝長加寬」被新程式直接沿用。現在 `gates.GATE_RULES_VERSION` 由手動標籤（`gate1-v3`）加上決定驗收的模組（`gates.py`、`markers.py`、`contracts.py`、`agent.py`）的雜湊組成，這些程式一改，版本就跟著變。它寫進三個地方：VerifiedSource 的快取鍵與儲存內容（讀取時版本不符就重新驗證）、預取導讀的快取鍵、導讀操作的 durable 節點紀錄鍵。代價：改動這些模組後，已快取的來源驗證與預取都會重做。
2. **「逐字相同」忽略有意義的差異**（已由第三次修正取代，見下一節）：
   - `_plain()` 移除所有標點，把 1.5 變成 15。現在數字之間的 `.`、`,`、`:` 保留（1.5 ≠ 15、1,000 ≠ 1000、1:2 ≠ 12）。
   - `verbatim_covered()` 以前只看正文是否「包含」命題原文。現在找出承載原文的完整句子，原文以外的部分只要有否定、條件、例外、量詞、可能性等標記，或「誤以為、所謂、迷思、並非、不是」這類框架詞，就不算程式確認，改送稽核。反例：「並非 X」「有人誤以為 X」「只有在正方形時，X」「X，這種說法不正確」「所謂「X」其實是迷思」都會送稽核。
3. **橋梁依據在 verify 被放寬**：verify 以前只要求理由非空。現在初次稽核與 verify 共用 `bridge_support_problem()`：依據原文的橋梁必須引用存在的原文句子編號（`S999` 不行，沒有編號也不行），其他依據要寫出推導或先備知識的理由。verify 的 prompt 改為顯示逐句編號的原文，並說明「只寫『這是正確的』不算依據」。固定使用舊 prompt bundle 的 session 仍拿到原文本身（沒有編號），程式檢查相同，所以缺依據的橋梁會被擋下而不是放行。

測試：後端 203 passed（新增 7 項），前端 33 pass、typecheck 通過。

### 第三次審查後修正（2026-10-04）

複查指出第二次修正仍有未涵蓋的路徑。三項都補了反例測試；**仍沒有用真模型重跑**。

1. **舊 checkpoint 續跑**：外層傳入新的操作鍵也沒用，LangGraph 續跑時子流程直接從 checkpoint 接續，沿用舊 `op` 與舊驗證。我先用小型巢狀 graph 與檔案 SQLite 確認：直接續跑會沿用舊狀態；先寫入外層狀態會產生新的 checkpoint，待執行的節點拿到新的 task id，子流程就從頭開始。修正：
   - 導讀子流程的狀態記錄 `gate_rules`。runner 在續跑前（`_run` 的兩個續跑分支）讀取子流程狀態，版本不符（包括沒有記錄的舊 checkpoint）時，把它已耗用的預算（取 checkpoint 與呼叫紀錄的較大值）寫進外層的 `guide_budget_carry`，再續跑；導讀從頭依新規則執行，沿用已耗用的預算。
   - 發布前（`accept`）再檢查一次，版本不符就不公開，記為 `gate_rules_changed`（可重試）。
   - 測試（獨立程序、檔案 SQLite）：舊規則下在 `draft` 前中斷，換新規則續跑後，`understand` 重新執行、公開的導讀標記新規則、預算計入兩次執行的呼叫。拿掉 runner 的修正時，發布前檢查會擋下（導讀不公開），測試失敗。
2. **正規化與語境快速路徑**：不再逐一補字元與句型，改成只接受能證明無損的差異。
   - `_plain()` 只忽略空白（中文字旁的空格、連續空白）與結尾的句末標點；數字、小數點、程式碼與數學符號、句中標點全部保留（`.5` ≠ `5`、`2**3` ≠ `23`、`不，是` ≠ `不是`、`x²` ≠ `x2`）。代價：模型只改了句中標點的命題也會送稽核。
   - 正文的程式涵蓋只接受「整段都是完整原文句子」的段落；段落中只要有一句不是原文（加了「並非」、後一句反駁、「原文指出：」這類引言），該命題就送稽核。其他段落的內容（包括另一段的反駁）仍由稽核以新增內容檢查。第二次修正加的框架詞清單已移除。
3. **非原文型橋梁的依據**：依 basis 要求可核對的內容，稽核與 verify 共用同一個檢查：
   - `source`：存在的原文句子編號；
   - `derivation`：作為前提的原文句子編號，加上推導步驟；
   - `prior_reading`：讀者已讀的命題，格式「單元:命題編號」，必須在讀者讀過的內容中；
   - `user_authorized`：用「」引用讀者提出的要求原話，必須出現在讀者的目標或背景中；
   - `prerequisite`：說明是什麼知識、為何是標準內容，去掉「正確、先備知識、常識」這類判語後仍要有內容。程式無法核對原文以外的知識，所以通過時只記為模型判斷（`bridge_checked_by_model`）。

   正文稽核與 verify 的 prompt 新增「橋梁可引用的依據」（已讀命題與編號、讀者的要求），並寫明各類型的格式。固定使用舊 prompt bundle 的 session 不送這一項，程式檢查相同，缺依據時擋下。

測試：後端 206 passed。

### 第四次審查後修正（2026-10-04）

複查再找出三個可重現的邊界，都以確定性測試補上；仍沒有用真模型重跑。

1. **程式碼與字串內的空白**（P1）：`Python 字串 "a  b" 的長度是 4` 改成 `"a b"` 仍走快速路徑。`_plain()` 現在先找出逐字比較的區段：圍欄與行內程式碼、數學式、字串常值（ASCII、彎引號、「」『』）、縮排的程式行；空白正規化與句末標點去除只作用在這些區段以外的散文。
2. **承接預算後的呼叫數**（P2）：承接 2 次、新流程呼叫 5 次（其中一次連線失敗後續跑），原本記 6 次。原因是取 `max(累計預算, 新 op 的呼叫紀錄)`，兩者範圍不同。現在子流程狀態分開保存承接基數 `budget_base`，以「承接基數＋本次 op 的全部呼叫紀錄（含失敗）」對帳；runner 切換版本時用同一個算法。用舊算法重跑測試會得到 6，修正後為 7。
3. **已讀引用的有效性**（P2）：只要有一個引用有效就通過，允許夾帶 `u-never-read:C999`。現在每個「單元:命題編號」都必須在讀者讀過的內容中，並列出無效的編號；讀者授權的「」引用也改為每一段都必須是讀者原話。

測試：後端 209 passed。

### 本輪限制與下一步

- 真模型重跑已完成（r9）；接著依 `RUBRIC.md` 為 `r9-compare-*` 盲評包評分（主要正文先評、中文課文另評、未公開草稿另審），以 `scripts/eval_reading.py compare … --scores scores.json` 揭盲；結果同時包含配對分數與誤擋數。這是決定能否切換預設的必要條件。
- 速度：可在不削弱關卡的前提下嘗試（皆需以同一驗收集重測）：
  - 正文稽核以分批方式稽核（「逐字寫出的命題不送稽核」已完成）；
  - 正文 `maxLength` 已加入，尚未以真模型重跑量測效果；
  - 讓預取在讀者閱讀時先跑完 Gate 1（VerifiedSource 可跨 session 重用）。
- 稽核引用：小模型常合併兩句後引用，造成「引用的句子不在主要正文」；可改成要求逐句片段，或允許引用多個片段再由程式逐一展開。
- 橋梁正確性仍只靠同一個 4b 模型核對：實測有一個核准橋梁把折射率寫成 v/c（應為 c/v），稽核沒有指出。
- 書籍專屬表記（`<BOOK>_Notation.md`）沒有導入；目前以作者 LaTeX 或 PDF 視覺取證為準。
- OpenVINO GPU（B70）、8b 模型、掃描 PDF（OCR）都沒有驗證或實作。
- 多 worker、`cross-page-condition` 這類沒有原始檔的頁面 fixture 的取證，維持現況。


## 提交紀錄

使用者授權後，在 `feat/reading-langgraph`（由 `develop` 建立）上依邏輯拆成 6 個 commit；未合併、未推送、未升版。P2–P4 共用 `db.py`（migration v2–v4）與 `repository.py`，所以沒有拆成各階段分支。第 1–5 個 commit 都在只含到該 commit 為止內容的工作樹上跑過測試（第 1 個用暫時的 git worktree 檢出測試）；第 6 個只有文件。

1. `fix(backend)`：Ollama qwen3 think 預填（36 passed）
2. `test(backend)`：固定教材集、gold labels、rubric、評估腳本（45 passed）
3. `feat(backend)`：依賴與 lock、LangChain bridge、runtime、tasks（60 passed）
4. `feat(backend)`：來源區塊、切段、導讀子流程、學習流程、events API、預取（117 passed）
5. `feat(frontend)`：events API、命令重送、階段顯示、i18n（typecheck 通過、30 pass）
6. `docs`：本進度文件

未提交：使用者原有的 `frontend/src/assets/companions/*.webp`、`STYLE.md`，以及 `docs/reading-agent-langgraph-plan.md`、`docs/claude-code-reading-agent-handoff.md`（本文件開頭連到規劃文件，該檔仍未納入版控）。

R0–R8 的改動之後經使用者授權，已在 `feat/reading-method-alignment` 上提交（`d5984a7` 起，見 `git log master..HEAD`）；未合併、未推送、未升版。以下是當時建議的拆分，保留供參考。建議的拆分（依 CONTRIBUTING.MD，各自可測）：

1. `test(backend)`：放行漏洞 regression fixtures 與 R0 基線腳本
2. `fix(backend)`：正文與中文課文分開驗收、移除相似度與標籤降級、嚴格審稿 schema（R1）
3. `feat(backend)`：模型能力宣告、輸入預算與 ContextPacket、工具的實際來源與完整性（R2）
4. `feat(backend)!`：來源先驗證的導讀子流程 reading-graph-v2（契約、Gate 1／Gate 2、橋梁、學習狀態、VerifiedSource 快取、JSON 的 LaTeX 反斜線修復）與 prompts（R3）
5. `feat(backend)`：原書結構、論證關係與小節邊界的 Part（R4，migration v5）
6. `feat(backend)`：PDF 版面與字型證據、視覺取證與未確認狀態（R5）
7. `feat(backend)`：已驗證筆記與依據綁定的出題（R6）
8. `feat(backend)`：舊 session 升級、匯出、預取採用、評估與量測腳本（R7／R8）
9. `feat(frontend)`：核對階段、白話的未公開原因與 i18n（R7）
10. `docs`：README、CHANGELOG、本進度文件

第 4 項標 `!`：reading-graph-v2 改變新版流程 session 的導讀契約與 prompt bundle；舊 session 由升級步驟處理（見 R7）。
