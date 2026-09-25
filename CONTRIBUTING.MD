# 貢獻與版本控管規範

本文件定義 mentora 專案採用的 Git 版本控管與發布規範。當專案操作者要求 Agent 協助規劃 branch name、commit message、版本號或 version tag 時，Agent 應依據本文件提出建議；實際版本號與發布時點仍由使用者決定。

## 分支用途

- repository 的預設分支固定為 `master`，用於保存已確認可正式發布或可作為穩定基準的內容。
- 使用 `develop` 作為整合分支。已完成但尚未正式發布的功能，應先合併至 `develop`。
- 每項範圍清楚、可獨立審查與合併的開發目標，應由 `develop` 建立短期工作分支，並依性質使用 `feat/`、`fix/`、`docs/` 或 `test/` 前綴，例如 `feat/companion-persona`。
- 即使多項需求同屬一個較大的功能領域，只要能夠分別開發、測試或發布，仍應建立不同的工作分支。
- 工作分支完成並合併後，原則上不再承載下一項開發目標；確認不再需要後即可刪除。
- 合併至 `develop` 代表完成整合；合併至 `master` 才代表該內容已進入正式發布基準。

## 變更性質與 commit

- 凡以自然語言撰寫、供模型或 Agent 執行或遵循，且會直接影響專案功能或行為的內容，均視為專案原始碼。例如 `backend/app/prompts/` 的提示詞、`backend/app/strategies/` 的教學策略、`agent/` 的工具說明。
- Prompt、Instruction、Skill、Workflow、Spec 或其他行為規範的變更，應依其實際功能性質分類，不得僅因採用 Markdown 等文件格式而歸類為 `docs`。
- 介面文字字典（`frontend/src/i18n/`）屬於功能的一部分：新增或修改中文字串時，同一個 commit 內應以 `bun run i18n:translate` 同步更新英文翻譯。
- 每個 commit 只包含一項邏輯完整的變更，不混入無關檔案。
- 同一個開發目標可以包含多個範圍清楚、可獨立說明的 commit。
- README、CHANGELOG 與功能文件可以和其所屬功能一起提交；若屬於獨立的發布整理工作，也可以建立單獨的 release preparation commit。

### Commit scope

子專案的變更一律使用該子專案的固定 `scope`，與專案層級的變更區分開來：

| 目錄 | 職責 | commit scope |
| --- | --- | --- |
| `backend/` | FastAPI 後端：切段、導讀、教學流程、模型呼叫、紀錄 | `backend` |
| `frontend/` | React 前端：學習畫面、學習紀錄、設定視窗、介面語系 | `frontend` |
| `agent/` | 以 pi 為基礎的閱讀 agent（選用） | `agent` |

- 同時橫跨多個子專案、且無法拆成獨立 commit 的變更（例如新增一支 API 並同時接上畫面），以主要承載行為的一方為 scope；若兩邊份量相當則省略 scope。
- 根目錄的啟動腳本（`scripts/`）、README、CONTRIBUTING 等專案層級變更不加 scope。

### Commit message 格式

Commit message 採用 Conventional Commits 格式：

```text
<type>[optional scope][!]: <description>

[optional body]

[optional footer]
```

- `type` 使用小寫英文，依變更的實際性質選擇：
  - `feat`：新增功能或可辨識的能力。
  - `fix`：修正錯誤、規範缺漏或不正確行為。
  - `docs`：只修改說明文件，且不改變程式或模型行為。
  - `refactor`：調整內部結構，但不新增功能也不修正錯誤。
  - `test`：新增或調整測試內容。
  - `chore`：其他不屬於上述類型的維護工作；發布準備使用 `chore(release)`。
- `description` 使用繁體中文，以簡潔的祈使語氣說明變更目的，不加句號。
- 小型且可由 subject 完整理解的 commit 可以省略 body；其餘 commit 應加入 body，說明變更目的、主要行為及重要限制或相容性影響。
- subject 與 body 之間保留一個空行。Body 不重複 subject，也不逐項抄錄檔案清單。
- 具有不向下相容影響的變更，應在 `type` 或 `scope` 後加入 `!`，並於 footer 使用 `BREAKING CHANGE:` 說明受影響的既有使用方式及必要調整。本專案常見的破壞性變更包括：API 路徑或回應格式改變、SQLite schema 改變而舊資料無法讀取、`MENTOR_*` 環境變數改名。
- Merge commit 保留 Git 產生的合併訊息，不套用上述格式。

範例：

```text
feat(frontend): 側邊欄加入設定視窗與伴讀角色
fix(backend): 導讀預取在段落切換時不再重複排程
chore(release): 準備 0.2.0
```

## 版本編號

- 整個 repository 共用一個版本號，記錄於根目錄 `CHANGELOG.MD`；`backend/pyproject.toml` 與各 `package.json` 的 `version` 在發布時一併對齊。
- 正式版本採用 Semantic Versioning，格式為 `MAJOR.MINOR.PATCH`，例如 `1.0.0`。
- Git version tag 在版本號前加上 `v`，例如版本 `1.0.0` 對應的 tag 為 `v1.0.0`。
- `MAJOR`：既有的執行方式、API 契約、資料格式或主要行為發生不向下相容的變更。
- `MINOR`：新增向下相容的功能或能力，既有使用方式仍可繼續成立。
- `PATCH`：修正錯誤、規範缺漏或不正確行為，且未增加新的功能能力。
- 功能是否位於相同領域，不是判斷 PATCH 或 MINOR 的依據；只要新增可辨識的功能能力，原則上即屬於 MINOR。
- 版本號代表一次實際發布，不隨每個 commit 或每次合併自動增加。`MAJOR` 一律由操作者指示；Agent 不自行決定 `MINOR` 以上的升版，提出前先詢問操作者。
- 多項功能若合併成同一次發布，只使用一個共同版本號；若分別正式發布，才分別建立對應版本。
- 若需要在正式發布前進行明確的預發布測試（例如比賽 demo 前），可依需要使用 `alpha`、`beta` 或 `rc` 標識，例如 `0.2.0-rc.1`；是否採用預發布版本由使用者決定。

## 不納入版控的內容

- 執行期資料：`backend/data/`（SQLite、匯入的教材、導讀工作區）。
- 模型權重：`backend/models/`。
- 依賴與建置產物：`node_modules/`、`backend/.venv/`、`frontend/dist/`、各種快取。
- 例外：`frontend/src/i18n/en.source.json` 是翻譯腳本判斷哪些 key 需要重翻的依據，**需要**納入版控。

## README 更新原則

- 當專案功能、使用方式、執行入口、必要輸入、主要輸出、API、環境變數、專案狀態或版本政策發生改變時，應同步更新 `README.md`。
- README 應在相關功能合併前完成更新，避免已完成的功能與專案說明不一致。
- 不要求每個 commit 都修改 README；只有使用者或開發者需要理解的專案現況發生改變時才需要更新。
- 若 README 的變更直接屬於某項功能，可以包含在該功能的 commit 中；若是跨功能的整體說明或發布整理，也可以建立獨立的文件或 release preparation commit。

## CHANGELOG 更新原則

- `CHANGELOG.MD` 用於記錄各版本中對使用者具有意義的變更，不直接複製完整 commit 紀錄。
- 開發期間，每次 commit 同步把對使用者有意義的差異補進 `[Unreleased]`。
- 應優先記錄新增功能、既有功能改變、錯誤修正、移除項目、相容性變更及其他會影響使用者的內容。
- 已被捨棄、未曾正式發布的中間設計或測試調整，不需要逐項記入正式版本紀錄。
- 正式發布時，應將本次發布內容由 `[Unreleased]` 整理至對應的版本段落，並以 `YYYY-MM-DD` 格式標示發布日期，例如：

```markdown
## [Unreleased]

## [0.1.0] - 2026-09-25
```

- 每次發布完成後，應保留新的 `[Unreleased]` 區段，供後續開發持續累積變更紀錄。

## 發布前檢查

合併至 `develop` 或 `master` 前，至少確認：

```bash
cd backend && .venv/bin/python -m pytest -q
cd frontend && bunx tsc --noEmit && bun test
```

## 正式發布流程

正式發布原則上依下列順序進行：

1. 在工作分支完成開發、文件更新與必要測試。
2. 將工作分支合併至 `develop`。
3. 在 `develop` 完成整合確認與發布前檢查。
4. 確認正式版本號，整理 README 與 CHANGELOG，必要時建立 release preparation commit。
5. 將已確認可發布的 `develop` 內容合併至 `master`。
6. 在 `master` 中代表正式發布內容的 commit 建立 version tag。

- 小型或單人維護的版本，原則上不強制建立獨立的 release branch。
- 若發布準備期較長、需要多人協作，或需要在功能開發持續進行時凍結發布內容，可再決定是否使用 `release/<version>` 分支。

## Version Tag 規範

- Version tag 只用於標記實際正式發布的版本，不用於標記一般 commit、工作分支完成或單純合併至 `develop`。
- 每個正式發布版本只建立一個對應 tag。
- Tag 應建立在 `master` 中代表該次正式發布內容的 commit 上。
- 正式發布使用 annotated tag，例如：

```bash
git tag -a v0.1.0 -m "Release v0.1.0"
```

- 已對外發布的 tag 原則上不移動或覆寫；若發布後發現問題，應以新的 PATCH、MINOR 或 MAJOR 版本修正。
