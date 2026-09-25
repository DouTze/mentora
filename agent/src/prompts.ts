// Task prompts. Same content contract as backend/app/prompts/{guide,ask,compact}.md, but phrased for an
// agent that fetches the text itself: it is told *which* quote to work on, not handed the text.
import type { Workspace } from "./workspace";

export const SYSTEM = `你是「導讀 agent」：你把教材改寫成一本直接以繁體中文寫成的教科書，並用工具讀取需要的段落。
工作方式：
- 先用工具讀你需要的內容（read_quote 讀原文；read_guide 承接前面幾段；read_compact 看已讀部分的筆記），不要憑記憶或猜測。
- 只根據原文寫；原文沒有的知識不要加，需要背景時給最少量並標示它不在原文中。
- 聲音：教材內部的直接敘述，讓學生感覺在讀原本就用中文寫的課本，而不是站在書外聽轉述。不用「作者在這裡說明」「原文指出」「這一段的重點是」這類外部旁白當框架；不叫學生回去看原文；不依賴「如上所述」。
- 完整性：原文的定義、主張、條件、推理、例子、限制、結論與公式必須完整保留，不得精簡刪減；保留原文的命題力度（必要／可能／禁止、確定程度、適用範圍）。
- 最小充分教學：只補足理解所必要的橋梁；超出原文主線的額外背景用「**補充說明：**」標示。
- 數學一律用 KaTeX：行內 $…$、獨立公式 $$…$$，反斜線完整保留，忠實保留大小寫、粗體與上下標。
- 一律使用繁體中文（台灣用語）。
- 完成後**只輸出一個 JSON 物件**作為最終回答，不要加任何說明文字或 Markdown 標記。`;

export function guideTask(ws: Workspace, index: number, prevPoints: string[], sourceIsChinese: boolean | undefined): string {
  const m = ws.manifest;
  const q = m.quotes[index];
  return `教材「${m.material_title}」${m.range_label} 共 ${m.quotes.length} 段，請為第 ${index + 1} 段（${q.label}${q.heading ? `，標題：${q.heading}` : ""}）寫導讀。

步驟：
1. 用 read_quote 讀第 ${index + 1} 段原文。${index > 0 ? `需要時用 read_guide 讀第 ${index} 段的導讀，承接已建立的概念。` : ""}
2. 寫出：
   - chinese_text（中文課文）：把原文忠實翻成自然的繁體中文，依原文順序、不增不減。${sourceIsChinese === false ? "這一段原文不是中文：chinese_text **必填**，不得為 null。" : sourceIsChinese ? "這一段原文已經是中文：填 null。" : "原文本來就是中文時填 null。"}
   - guide（主要正文）：學生真正要讀的課文，將**取代**這段原文。用教材內部的直接敘述聲音，完整交代這段的定義、主張、條件、公式、例子與結論，讓學生不看原文也能完整學到；只補足必要的橋梁（先備概念、中間推理、符號讀法、最少量例子、易混淆處），不延伸到原文主線之外。Markdown，分 2–6 個自然段，段落之間用空行分隔；可用條列、粗體、表格；不加標題、不寫本段摘要或下段預告。
   - key_points：這段建立的 1–3 個重點，一句一個。
   - check：一個讓學生自我檢查的問句。
${prevPoints.length ? `\n前面幾段已建立的重點：\n${prevPoints.map((p) => `- ${p}`).join("\n")}\n` : ""}
最終只輸出：{"chinese_text":"…或 null","guide":"…","key_points":["…"],"check":"…"}`;
}

export interface QAHistory {
  question: string;
  answer: string;
  quote_index?: number;
  quote_label?: string;
}

export function askTask(ws: Workspace, index: number, question: string, history: QAHistory[]): string {
  const m = ws.manifest;
  const q = m.quotes[index];
  const hist = history
    .map((h) => {
      const where = h.quote_index === undefined || h.quote_index === index ? "這一段" : `第 ${h.quote_index + 1} 段（${h.quote_label ?? ""}）`;
      return `［${where}］學生：${h.question}\n　導師：${h.answer}`;
    })
    .join("\n");
  return `學生正在讀教材「${m.material_title}」第 ${index + 1} 段（${q.label}），有一個問題。

步驟：
1. 用 read_quote 讀第 ${index + 1} 段原文，用 read_guide 讀這段的導讀。學生先前在別段問過的問題若和這題有關，可用 read_guide 讀那一段的導讀來承接。
2. 只根據原文與課文回答，2–5 句，用教材內部的直接敘述把該說的說完，可舉一個小例子；不叫學生回去看原文；沒談到的要明說並標示不在課文中。
3. 參考先前的問答：已經解釋過的概念直接承接（「你在第 2 段問過……，這裡的情況是……」）；同一個誤解再出現時指出它和前面問題的關聯，不要當作第一次聽到。
${hist ? `\n本次學習到目前為止的問答（「這一段」= 目前這段）：\n${hist}\n` : ""}
學生的問題：${question}

最終只輸出：{"answer":"…"}`;
}

export function segmentTask(materialTitle: string, atoms: { heading?: string | null; text: string }[]): string {
  const listing = atoms.map((a, i) => `【${i + 1}】${a.heading ? `（小標題：${a.heading}）\n` : ""}${a.text}`).join("\n\n");
  return `你正在為教材「${materialTitle}」規劃精讀講義的來源單元。下面是依段落切好的 ${atoms.length} 個候選段落（依原文順序編號）。請把它們組成正式的來源單元：每個單元之後會各自得到一段中文課文和一段取代原文的主要正文，所以顆粒度要讓每個單元都能支撐一段有意義的正文。

劃分規則：
- 預設保留原書的一個段落，或一組連續、共同完成同一論述目的的段落，作為一個單元。
- 同一段落裡有多個彼此相關的定義、性質、例子或平行項目時，不因項目多就拆開；不以單一句子、單一術語為單位。
- 短句、過場句或局部定義若不足以支撐有意義的正文，併入相鄰且語意相關的段落。
- 只有原書出現段落切換、小標題、獨立插入內容（Figure／Table／Algorithm／Code／Example／Footnote）或明確論述轉折，或單一單元會涵蓋過多不相干內容時，才開始新的單元。
- 公式與它的引入、條件、where 說明及後續結論保持在同一單元；獨立插入內容自成單元。
- 不切斷同一清單、同一例子、同一反例或同一完整論證；不以固定字數機械切割。
- 單元依原文順序、連續、不重疊，且每個候選段落恰好用到一次。

候選段落：
${listing}

最終只輸出：{"units":[{"atoms":[1],"title":"…"},{"atoms":[2,3],"title":null}]}（atoms 是連續遞增的候選段落編號；title 為 8 字內短標題，可為 null）`;
}

export function compactTask(ws: Workspace, part: number, quoteTo?: number): string {
  const m = ws.manifest;
  const p = { ...m.parts[part] };
  if (quoteTo !== undefined && quoteTo < p.quote_to) {
    p.quote_to = quoteTo; // the student stopped early: only the quotes actually read
    p.label += `（讀到第 ${quoteTo + 1} 段）`;
  }
  return `學生已讀完教材「${m.material_title}」第 ${part + 1} 部分（${p.label}，第 ${p.quote_from + 1}–${p.quote_to + 1} 段）。請把它壓縮成精簡筆記；之後出題老師只看這份筆記、不再看原文，所以要保留出題所需的一切。

步驟：
1. 用 read_quote 依序讀第 ${p.quote_from + 1} 到第 ${p.quote_to + 1} 段原文（一段一段讀，不要跳過）。
2. 寫出：
   - summary：300–600 字，依原文順序涵蓋每個主題的定義、主張、條件、步驟、公式與結論。
   - claims：原文明確提出、可以拿來出題的主張或規則，每條一句，5–12 條。
   - key_terms：關鍵術語 5–12 個，術語保留原文寫法並附中文。

最終只輸出：{"summary":"…","claims":["…"],"key_terms":["…"]}`;
}
