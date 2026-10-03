# 導讀 Agent 與 LangGraph 改造進度

對應規劃：[reading-agent-langgraph-plan.md](reading-agent-langgraph-plan.md)。本文件記錄每階段的實際完成項、檢查結果、決策與限制。未執行的檢查不標成通過。

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

## 建議的下一步

1. 人工盲評：依 `RUBRIC.md` 填 `scores.json`，執行 `scripts/eval_reading.py compare data/evals/p0-ollama-4b-baseline data/evals/p3-ollama-4b-candidate --scores scores.json`。
2. 降低耗時：把 analyze 併入 draft（常態 3 次 → 2 次）、只在程式驗證或規則判斷有疑慮時才呼叫檢查，再重跑對照，目標 p95 ≤ 3 倍。
3. 重跑 candidate 驗證檢查輸出上限修正，並量測預取後的首段等待時間。
4. 通過後才把預設切到 `langgraph`。

## 提交紀錄

使用者授權後，在 `feat/reading-langgraph`（由 `develop` 建立）上依邏輯拆成 6 個 commit；未合併、未推送、未升版。P2–P4 共用 `db.py`（migration v2–v4）與 `repository.py`，所以沒有拆成各階段分支。第 1–5 個 commit 都在只含到該 commit 為止內容的工作樹上跑過測試（第 1 個用暫時的 git worktree 檢出測試）；第 6 個只有文件。

1. `fix(backend)`：Ollama qwen3 think 預填（36 passed）
2. `test(backend)`：固定教材集、gold labels、rubric、評估腳本（45 passed）
3. `feat(backend)`：依賴與 lock、LangChain bridge、runtime、tasks（60 passed）
4. `feat(backend)`：來源區塊、切段、導讀子流程、學習流程、events API、預取（117 passed）
5. `feat(frontend)`：events API、命令重送、階段顯示、i18n（typecheck 通過、30 pass）
6. `docs`：本進度文件

未提交：使用者原有的 `frontend/src/assets/companions/*.webp`、`STYLE.md`，以及 `docs/reading-agent-langgraph-plan.md`、`docs/claude-code-reading-agent-handoff.md`（本文件開頭連到規劃文件，該檔仍未納入版控）。
