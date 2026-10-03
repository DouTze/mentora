"""Export a session as JSON or Markdown."""
from __future__ import annotations

import json


def to_json(view: dict) -> str:
    return json.dumps(view, ensure_ascii=False, indent=2)


def _workflow_lines(view: dict) -> list[str]:
    if view.get("workflow_runner") != "langgraph":
        return []
    wf = view.get("workflow") or {}
    return [f"- 流程：langgraph {wf.get('workflow_version')}（prompt bundle `{wf.get('prompt_bundle_hash')}`，"
            f"模型設定 `{wf.get('model_config_hash')}`，revision {view.get('revision')}）"]


def to_markdown(view: dict) -> str:
    mc = view["model_config_snapshot"]
    lines = [
        f"# 學習紀錄 {view['id']}",
        "",
        f"- 教材：{view['content_title']}（{view['content_id']} v{view['content_version']}）",
        *([f"- 範圍：{view['source']['material_title']} {view['source']['label']}（題目由模型依此範圍產生）"] if view.get("source") else []),
        f"- 教學策略：{view['strategy_id']} v{view['strategy_version']}",
        f"- 模型：{mc.get('backend')} / {mc.get('resolved_model') or mc.get('model')}"
        + ("（**模擬**）" if mc.get("simulated") else ""),
        f"- 生成設定：temperature={mc.get('temperature')}, max_tokens={mc.get('max_tokens')}, ctx={mc.get('context_tokens')}",
        f"- 狀態：{view['status']}　建立：{view['created_at']}　更新：{view['updated_at']}",
        *_workflow_lines(view),
        "",
    ]
    rd = view.get("reading")
    if rd:
        stopped = rd.get("stopped_at")
        lines += ["## 導讀", "", f"共 {rd['total']} 段"
                  + (f"，讀到第 {stopped + 1} 段就提前出題（之後的段落未導讀）" if stopped is not None else "")
                  + (f"，出題依據：{'精簡筆記' if rd.get('quiz_source') == 'compact' else '原文'}" if rd.get("quiz_source") else ""), ""]
        for q in rd["quotes"]:
            g = rd["guides"].get(str(q["index"]))
            lines.append(f"### {q['index'] + 1}. {q['label']}" + (f" · {q['heading']}" if q.get("heading") else ""))
            lines.append(f"> {q['excerpt']}")
            lines.append("")
            if g:
                if g.get("chinese_text"):
                    lines += ["**中文課文**", "", g["chinese_text"], ""]
                lines += [g["guide"], ""]
                if g.get("review_status"):
                    warn = "、".join(sorted({w["code"] for w in g.get("warnings") or []}))
                    lines += [f"_檢查：{g['review_status']}" + (f"（提醒：{warn}）" if warn else "")
                              + ("，經過一次修訂" if g.get("revised") else "") + "_", ""]
            else:
                lines += ["_（尚未導讀）_", ""]
            for qa in rd["qa"].get(str(q["index"]), []):
                lines += [f"**學生問**：{qa['question']}", f"**導師答**：{qa['answer']}", ""]
    lines += ["## 互動", ""]
    for t in view["turns"]:
        lines.append(f"### 第 {t['idx']} 輪 · {t.get('phase')} · {t.get('question_id')}")
        lines.append(f"**學生**：{t['student_answer']}")
        if t.get("error"):
            lines.append(f"**錯誤**：`{t['error']}`")
        else:
            j, d, m = t.get("judgement") or {}, t.get("decision") or {}, t.get("tutor_message") or {}
            lines.append(f"**判斷**：{j.get('assessment')}（reasoning_ok={j.get('reasoning_ok')}）— {j.get('evidence')}")
            lines.append(f"**規則**：{d.get('rule')} → **{d.get('action')}**")
            lines.append(f"**導師**：{m.get('feedback')}")
            if m.get("hint"):
                lines.append(f"**提示**：{m['hint']}")
            if m.get("probe"):
                lines.append(f"**追問**：{m['probe']}")
            if m.get("explanation"):
                lines.append(f"**教材解釋**：{m['explanation']}")
            if t.get("validation_notes"):
                lines.append(f"_驗證備註_：{'；'.join(t['validation_notes'])}")
        lines.append(f"_耗時 {t.get('latency_ms')} ms · tokens {json.dumps(t.get('usage') or {}, ensure_ascii=False)}_")
        lines.append("")
    s = view.get("summary")
    if s:
        lines += ["## 學習小結", "", f"結果：**{s['verdict']}**（{s['turn_count']} 輪，{s['error_count']} 次錯誤）", "",
                  "**練習過什麼**"]
        lines += [f"- {p['title']}：{p['status']}" for p in s["practiced"]]
        lines += ["", "**在哪裡卡住**"] + ([f"- {x['prompt']}" for x in s["stuck_at"]] or ["- 無"])
        lines += ["", "**接下來複習什麼**"] + ([f"- {x['title']}：{x['explanation']}" for x in s["review_next"]] or ["- 無"])
    return "\n".join(lines) + "\n"
