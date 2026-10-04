# 導讀評估 rubric

本 rubric 用於固定教材集（`manifest.json`）的人工評分。自動檢查（`evals/reading_eval.py`）只提供信號，品質以人工評分為準；LLM judge 可輔助但不能是唯一依據。

## 評分對象

每一題是一個 gold 來源單元，以及兩個 pipeline 為它產生的導讀（盲評時以 X／Y 隨機呈現）。若某 pipeline 把該單元與相鄰單元合併，評的是覆蓋該單元的整段導讀。

## 分開評分的兩個欄位

- **主要正文**：先只讀兩份主要正文評分。學生只讀正文，所以正文漏掉的定義、條件、公式或結論，即使中文課文有寫，也算正文漏掉。
- **中文課文**（原文非中文時才有）：讀完正文、評完分之後再看，另外評忠實性與完整性。中文課文寫得好，不能補正文的分數。
- **補充內容**：正文中原文以外的內容（含「補充說明」）另勾兩項：`supplement_incorrect`（內容不正確）、`supplement_unnecessary`（移除也不妨礙理解）。標成補充說明不代表可以不正確或不必要。

## 三個維度（1–5 分）

| 分數 | 忠實性 faithfulness | 完整性 completeness | 清楚度 clarity |
| --- | --- | --- | --- |
| 5 | 所有主張、條件、公式與原文一致；補充內容清楚標示 | gold `must_keep` 全部保留，沒有遺漏推理步驟 | 學生不看原文也能讀懂；用語自然、結構清楚 |
| 4 | 有輕微措辭偏差，但不改變命題力度 | 漏掉一項非關鍵細節 | 偶有冗句或轉折不順 |
| 3 | 有一處弱化或擴張原文主張 | 漏掉一項關鍵條件以外的重點 | 需要回頭重讀才能懂 |
| 2 | 多處偏差，或有一項無依據主張 | 漏掉多項重點 | 結構混亂或大量旁白 |
| 1 | 與原文矛盾 | 只剩片段 | 無法閱讀 |

## 重大錯誤（任一項成立即為重大錯誤，驗收要求為 0）

- `wrong_formula`：公式、符號、上下標、粗體向量與原文不一致，或 LaTeX 被破壞到改變意義。
- `fabricated_claim`：出現原文沒有、且未以「補充說明」標示的關鍵主張；或描述了抽取不到的圖片內容。
- `missing_key_condition`：漏掉 gold 標為 `condition` 的關鍵條件，或把「必要／可能／不一定」等命題力度改錯。

## 筆記與題目（R6，抽查）

- 筆記：每條筆記對照它標示的命題，勾 `note_lost_condition`（壓縮後掉了條件／例外／否定／公式）與 `note_fabricated`（說了依據沒有的內容）。
- 題目：每題對照它標示的依據，勾 `question_unanswerable`（依據不足以回答）、`question_wrong_condition`（新情境違反依據的條件）、`question_outside_read_range`（需要沒讀過的內容）。
- 以上任一成立都算重大錯誤。

## 驗收門檻（規劃 §10.2）

- 固定集主要正文三個維度平均各至少 4 分，主要正文與中文課文的重大錯誤皆為 0。
- 與舊版同模型、同參數、同來源比較：基準未達門檻的項目需改善，其餘不退步。基準已達門檻時，不宣稱品質提升，改報可追溯性／恢復改善。
- 第一輪至少 12 份新舊輸出盲評，覆蓋所有教材類型；最終驗收覆蓋完整固定集。代表性困難案例（`formula-where`、`cross-window-argument`、`cross-page-condition`）重跑 2 次檢查波動。

## 評分流程

1. `scripts/eval_reading.py baseline …` 與 `candidate …` 用同一個模型、溫度與 manifest 各跑一次。
2. `scripts/eval_reading.py compare <A> <B>` 產生 `blind/packet.md`、`blind/key.json`、`blind/scores.template.json`。
3. 評分者只看 `packet.md`，把 `scores.template.json` 複製成 `scores.json` 填分（不要打開 `key.json`）。
4. `scripts/eval_reading.py compare <A> <B> --scores scores.json` 揭盲並計算平均。

## 切段 gold labels 的意義

- `start`：gold 單元的起點片段；預設必須是候選切段的邊界。
- `merge_ok`：此單元可併入前一單元（允許的另一種切法）。
- `contains`：必須與 `start` 落在同一個候選單元的片段（例如定義與例子、公式與 where、跨頁條件）。
- `allowed_breaks`／`sentence_breaks_ok`：額外允許的邊界；後者允許任何句尾。
- 在 `$…$`、`$$…$$` 或程式碼區塊中間切開，一律視為違規。
- `must_keep`：導讀必須保留的主張／條件／公式；`any` 是可接受的關鍵詞組合（任一組全部出現即算保留）。這是自動信號，評分者仍以語意判斷。
- `forbidden`／`forbidden_exact`：不得出現的捏造內容，或被教材內指令文字操控的輸出。
