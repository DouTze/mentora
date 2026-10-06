"""Simulated backend: keyword rules only. Used to test screens and flow without a model.
Every result is explicitly marked simulated; it is never used as a fallback for a real backend."""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any

from .base import LLMBackend, LLMResult

GIVE_UP = ("不知道", "不懂", "不會", "不清楚", "沒想法")
DENY_CORRELATION = ("沒關係", "無關", "沒有關係", "不相關")


class MockBackend(LLMBackend):
    name = "mock"
    model = "keyword-rules"
    simulated = True

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None) -> LLMResult:
        t0 = time.perf_counter()
        meta = meta or {}
        answer: str = meta.get("student_answer", "")
        key_terms: list[str] = meta.get("key_terms", [])
        concept_id = meta.get("concept_id", "")
        hints: list[str] = meta.get("hints", [])
        hint_idx = min(meta.get("hint_count", 0), max(len(hints) - 1, 0))
        next_hint = hints[hint_idx] if hints else "想想有沒有第三個因素同時影響兩者？"

        step = meta.get("step")
        if meta.get("force_invalid_output"):
            text = "這不是 JSON"
        elif step == "segment_blocks":
            text = json.dumps(self._segment_blocks(meta.get("blocks", [])), ensure_ascii=False)
        elif step == "segment_split":
            text = json.dumps(self._segment_split(meta.get("blocks", [])), ensure_ascii=False)
        elif step == "segment_topic_summary":   # simulated: every paragraph on one topic (no understanding involved)
            text = json.dumps({"paragraphs": [{"topic": "（模擬）同一主題", "aspect": f"第 {k} 段"}
                                              for k in range(1, int(meta.get("paragraphs", 1)) + 1)]}, ensure_ascii=False)
        elif step == "segment_outline":   # simulated: a 區塊 under a heading is about that heading's subject
            n, heads, topic, out = int(meta.get("paragraphs", 1)), meta.get("headings") or [], "（模擬）同一主題", []
            for k in range(1, n + 1):
                topic = (heads[k - 1] if k - 1 < len(heads) else "") or topic
                out.append({"topic": topic, "aspect": f"第 {k} 段", "summary": f"（模擬）第 {k} 段"})
            text = json.dumps({"paragraphs": out}, ensure_ascii=False)
        elif step == "segment_topic":   # simulated: a new topic where the topic wording changes
            ts = meta.get("topics") or []
            text = json.dumps({"new_topic_at": [k for k in range(2, len(ts) + 1) if ts[k - 1] != ts[k - 2]]},
                              ensure_ascii=False)
        elif step == "reading_topic":
            text = json.dumps({"summary": "（模擬）這個主題的概要。", "key_points": ["（模擬）重點"],
                               "check": "你能用自己的話說出這個主題的重點嗎？"}, ensure_ascii=False)
        elif step == "context":
            text = json.dumps({"action": "ready", "calls": [], "reason": "（模擬）不需要補充"}, ensure_ascii=False)
        elif step in ("understand", "revise_source"):
            u = self._understand(meta.get("quote_text", ""), meta.get("segments") or [])
            if meta.get("evidence_formulas"):   # formulas read from the PDF region go with the first claim
                u["claims"][0]["formulas"] = list(meta["evidence_formulas"])
            text = json.dumps(u, ensure_ascii=False)
        elif step == "source_audit":
            text = json.dumps({"sentences": [{"id": sid, "verdict": "faithful", "note": "（模擬）"}
                                             for sid, s in meta.get("sentences") or []],
                               "claims": [{"id": c, "verdict": "faithful", "note": "（模擬）"} for c in meta.get("claim_ids") or []]},
                              ensure_ascii=False)
        elif step == "plan_bridges":
            text = json.dumps({"learning_goal": "（模擬）讀懂這一段", "bridges": []}, ensure_ascii=False)
        elif step in ("draft", "revise_body") and meta.get("claims") is not None:
            # simulated 主要正文: restates every claim — the source passage verbatim for a Chinese source, the audited
            # Chinese statement (with the source's formulas) otherwise, as a real guide must; no teaching
            body = "\n\n".join(self._restated(c) for c in meta["claims"])
            body += "".join(f"\n\n$${f}$$" for f in meta.get("evidence_formulas") or [])
            body += "".join(f"\n\n{b['content']}" for b in meta.get("bridges") or [])
            text = json.dumps({"guide": "（模擬導讀）以下逐句重述原文。\n\n" + body, "key_points": [self._first_sentence(body)[:40]],
                               "check": "你能用自己的話說出這一段的重點嗎？", "bridges_used": [], "limits": []}, ensure_ascii=False)
        elif step == "body_audit":
            text = json.dumps({"claims": [{"id": c["id"], "verdict": "covered", "guide_quote": self._restated(c), "note": ""}
                                          for c in meta.get("claims") or []],
                               "bridges": [{"id": b["id"], "verdict": "ok", "correct": "yes", "support": "S1",
                                            "guide_quote": b["content"], "note": ""} for b in meta.get("bridges") or []],
                               "additions": [], "uncovered_source": [], "coherence": "pass", "coherence_note": "（模擬）"},
                              ensure_ascii=False)
        elif step in ("notes", "notes_revise"):   # simulated notes: one per verified claim, its statement as is
            text = json.dumps({"notes": [{"id": f"N{i}", "kind": "point", "text": st, "claim_refs": [k]}
                                         for i, (k, st) in enumerate((meta.get("claims_map") or {}).items(), 1)]},
                              ensure_ascii=False)
        elif step == "notes_audit":
            text = json.dumps({"notes": [{"id": i, "verdict": "faithful", "note": ""} for i in meta.get("note_ids") or []]},
                              ensure_ascii=False)
        elif step == "quiz_generate":
            cs = []
            for t in meta.get("plan") or []:
                b = t["basis"][:2]
                q = lambda kind: {"prompt": f"（模擬出題）請說明「{t['label'][:20]}」的{kind}並說明理由。",
                                  "rubric": "回答需涵蓋依據中的條件與結論。", "hints": ["先回想依據的第一個重點？", "條件是什麼？"],
                                  "basis": b}
                cs.append({"topic": t["id"], "title": t["label"][:20], "explanation": "（模擬）依已核對的筆記整理。",
                           "examples": [], "key_terms": self._terms(t["label"], 3), "basis": b,
                           "diagnostic": q("重點"), "transfer": q("應用")})
            text = json.dumps({"title": "（模擬）已讀範圍", "concepts": cs}, ensure_ascii=False)
        elif step == "quiz_audit":
            text = json.dumps({"questions": [{"id": i, "verdict": "ok", "note": ""} for i in meta.get("question_ids") or []]},
                              ensure_ascii=False)
        elif step == "analyze":
            first = self._first_sentence(meta.get("quote_text", ""))
            text = json.dumps({"definitions": [], "claims": [first], "conditions": [], "formulas": [], "reasoning": [],
                               "prerequisites": [], "needs_context": [], "limits": []}, ensure_ascii=False)
        elif step in ("draft", "revise"):
            g = self._guide(meta.get("quote_text", ""))
            g["guide"] = g["guide"].replace("$", "")   # the restated sentence may be cut inside a formula
            maths = re.findall(r"\$\$.+?\$\$|\$[^$\n]+\$", meta.get("quote_text", ""), re.S)
            if maths:   # the simulated text keeps the formulas verbatim, as a real guide must
                g["guide"] += "\n\n" + "\n\n".join(maths)
            text = json.dumps({**g, "supplement_notes": [], "limits": []}, ensure_ascii=False)
        elif step == "verify":   # simulated re-check cannot read; it keeps every flagged problem
            text = json.dumps({"checks": [{"item": 1, "verdict": "problem", "guide_quote": "", "reason": "（模擬）無法核對"}]},
                              ensure_ascii=False)
        elif step == "review":   # simulated review reports every expected check, finds nothing
            text = json.dumps({"checks": [{"check": c, "result": "pass", "evidence": "（模擬）"} for c in meta.get("expected_checks", [])],
                               "issues": []}, ensure_ascii=False)
        elif meta.get("task") == "generate":
            text = json.dumps(self._generate(meta.get("range_text", ""), meta.get("reading_questions", [])), ensure_ascii=False)
        elif meta.get("task") == "guide":
            text = json.dumps(self._guide(meta.get("quote_text", "")), ensure_ascii=False)
        elif meta.get("task") == "ask":
            text = json.dumps({"answer": f"（模擬回答）這段原文提到：{self._first_sentence(meta.get('quote_text', ''))}"
                                         f"你問的是「{meta.get('question', '')[:40]}」，可以回頭對照這一句。"}, ensure_ascii=False)
        elif meta.get("task") == "segment":
            text = json.dumps(self._segment(meta.get("atoms", [])), ensure_ascii=False)
        elif meta.get("task") == "compact":
            text = json.dumps(self._compact(meta.get("part_text", "")), ensure_ascii=False)
        else:
            j = self._judge(answer, key_terms, next_hint, concept_id)
            text = json.dumps(j, ensure_ascii=False)
        latency = int((time.perf_counter() - t0) * 1000)
        return LLMResult(text=text, latency_ms=latency, usage={"simulated": True}, raw=None)

    @staticmethod
    def _judge(answer: str, key_terms: list[str], next_hint: str, concept_id: str) -> dict[str, Any]:
        a = answer.strip()
        base = {"probe": None, "hint": None, "source_ids": [concept_id]}
        if len(a) < 4:
            return {**base, "assessment": "unclear", "reasoning_ok": False,
                    "evidence": "回答過短，無法判斷", "feedback": "可以多說一點嗎？",
                    "probe": "你認為這兩件事之間是什麼關係？為什麼？"}
        if any(k in a for k in GIVE_UP):
            return {**base, "assessment": "incorrect", "reasoning_ok": False,
                    "evidence": "學生表示不知道", "feedback": "沒關係，我們一步一步來。", "hint": next_hint}
        if any(k in a for k in key_terms):
            return {**base, "assessment": "correct", "reasoning_ok": True,
                    "evidence": f"學生提到關鍵因素：{[k for k in key_terms if k in a][0]}",
                    "feedback": f"很好，你提到了關鍵：{[k for k in key_terms if k in a][0]}。"}
        if any(k in a for k in DENY_CORRELATION):
            return {**base, "assessment": "correct", "reasoning_ok": False,
                    "evidence": "學生否認兩者有關聯，但資料顯示確實相關",
                    "feedback": "結論方向是對的，但理由需要再想想：資料確實顯示兩者一起變動。",
                    "probe": "如果兩者真的一起變動，那為什麼還不能說是因果？"}
        if "同意" in a and "不同意" not in a:
            return {**base, "assessment": "incorrect", "reasoning_ok": False,
                    "evidence": "學生接受了因果主張", "feedback": "先別急著接受這個結論。", "hint": next_hint}
        return {**base, "assessment": "partial", "reasoning_ok": False,
                "evidence": "學生指出兩件事一起發生，但未排除共同原因",
                "feedback": "你已經找到兩者的關聯，還需要想想其他可能原因。", "hint": next_hint}

    @staticmethod
    def _first_sentence(text: str) -> str:
        flat = re.sub(r"\s+", " ", text).strip()
        return (re.split(r"(?<=[。！？.!?])", flat, maxsplit=1)[0] or flat)[:80]

    @staticmethod
    def _terms(text: str, n: int = 5) -> list[str]:
        grams = Counter(m for run in re.findall(r"[\u4e00-\u9fff]{2,}", text) for m in (run[i:i + 2] for i in range(len(run) - 1)))
        words = Counter(w.lower() for w in re.findall(r"[A-Za-z]{4,}", text))
        return [t for t, _ in (grams + words).most_common(n)]

    @staticmethod
    def _restated(c: dict) -> str:
        src = c.get("source_text") or ""
        if len(re.findall(r"[一-鿿]", src)) >= len(re.findall(r"[A-Za-z]", src)) or not c.get("statement"):
            return src
        maths = [m for m in re.findall(r"\$\$.+?\$\$|\$[^$\n]+\$", src, re.S) if m not in c["statement"]]
        return c["statement"] + "".join(f" {m}" for m in maths)

    @classmethod
    def _guide(cls, text: str) -> dict[str, Any]:
        """Simulated 導讀: restates the paragraph; no understanding involved."""
        terms = cls._terms(text)
        first = cls._first_sentence(text)
        latin = len(re.findall(r"[A-Za-z]", text)) > len(re.findall(r"[\u4e00-\u9fff]", text))
        return {"chinese_text": f"（模擬中文課文）{first}" if latin else None,
                "guide": f"（模擬導讀）{first}\n\n請注意這幾個詞：{'、'.join(terms) or '（無）'}。"
                         f"它們之間的關係，以及成立的條件，是接下來要處理的。",
                "key_points": [first[:40]] + terms[:2], "check": f"你能用自己的話說出「{first[:20]}」是什麼意思嗎？"}

    @staticmethod
    def _understand(text: str, segments: list) -> dict[str, Any]:
        """Simulated source claims: one claim per source sentence, quoting it verbatim (so the program's anchor and
        coverage checks see a complete list); the 中文課文 of a non-Chinese text is the text itself, marked."""
        segs = segments or [["S1", text]]
        latin = len(re.findall(r"[A-Za-z]", text)) > len(re.findall(r"[\u4e00-\u9fff]", text))
        def zh(s: str) -> str:   # simulated translation: the words joined by ・ (markers and formulas kept, not the source verbatim)
            parts = re.split(r"(\$\$.+?\$\$|\$[^$\n]+\$)", s, flags=re.S)
            return "（模擬譯文）" + "".join(p if p.startswith("$") else re.sub(r"\s+", "・", p) for p in parts)

        return {"translations": [{"id": sid, "zh": zh(s)} for sid, s in segs] if latin else [],
                "claims": [{"id": f"C{i}", "sentences": [sid], "statement": "", "function": "claim", "conditions": [],
                            "exceptions": [], "strength": "", "formulas": re.findall(r"\$\$.+?\$\$|\$[^$\n]+\$", s, re.S),
                            "depends_on": []} for i, (sid, s) in enumerate(segs, 1)],
                "needs_context": [], "limits": []}

    @staticmethod
    def _segment(atoms: list[str]) -> dict[str, Any]:
        """Simulated 來源單元 planning: one unit per candidate paragraph, a short one joins the previous."""
        units: list[dict[str, Any]] = []
        for i, a in enumerate(atoms, 1):
            if units and len(a) < 60:
                units[-1]["atoms"].append(i)
            else:
                units.append({"atoms": [i], "title": None})
        return {"units": units}

    @staticmethod
    def _segment_blocks(blocks: list[dict]) -> dict[str, Any]:
        """Simulated block planning: follows the program's glue marks, a heading opens a unit, a sentence of the
        same rebuilt PDF paragraph or a short paragraph joins the previous one. No understanding involved."""
        units: list[dict[str, Any]] = []
        glued = False
        for b in blocks:
            join = units and (glued or (b["kind"] != "heading" and (b["chars"] < 40 or b.get("same_para"))
                                        and units[-1]["blocks"]))
            if join and not (b["kind"] == "heading" and not glued):
                units[-1]["blocks"].append(b["label"])
            else:
                units.append({"blocks": [b["label"]], "title": None, "reason": "（模擬）新段落"})
            glued = b.get("glue_next", False) or b["kind"] == "heading"
        return {"units": units}

    @staticmethod
    def _segment_split(blocks: list[dict]) -> dict[str, Any]:
        """Simulated focused split: two halves, cut at the first boundary past the middle that is not glued."""
        labels = [b["label"] for b in blocks]
        cut = next((k for k in range(len(blocks) // 2, len(blocks)) if k and not blocks[k - 1]["glue_next"]), None)
        if cut is None:
            return {"units": [{"blocks": labels, "title": None, "reason": "（模擬）無法拆開"}]}
        return {"units": [{"blocks": labels[:cut], "title": None, "reason": "（模擬）前半"},
                          {"blocks": labels[cut:], "title": None, "reason": "（模擬）後半"}]}

    @classmethod
    def _compact(cls, text: str) -> dict[str, Any]:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not p.startswith("[")]
        claims = [cls._first_sentence(p) for p in paras][:12]
        return {"summary": "（模擬精簡筆記）" + " ".join(claims)[:600], "claims": claims, "key_terms": cls._terms(text, 8)}

    @staticmethod
    def _generate(text: str, reading_questions: list[str] | None = None) -> dict[str, Any]:
        """Simulated question writer: no understanding, just slices the range and counts frequent terms.
        Questions the student asked while reading are folded into the first diagnostic prompt."""
        body = re.sub(r"\[第 \d+ 頁\]", "", text)
        paras = [re.sub(r"^[#>\-*\s]+", "", p).strip() for p in re.split(r"\n\s*\n|\n(?=#)", body)]
        paras = [p for p in paras if len(p) >= 2] or [body.strip()]  # short ones are usually headings → titles
        half = (len(paras) + 1) // 2
        chunks = [paras] if len(paras) < 4 else [paras[:half], paras[half:]]
        concepts = []
        for chunk in chunks:
            joined = " ".join(chunk)
            title = re.split(r"[。．.!?！？：:\n]", chunk[0])[0][:20] or "重點"
            grams = Counter(m for run in re.findall(r"[\u4e00-\u9fff]{2,}", joined) for m in (run[i:i + 2] for i in range(len(run) - 1)))
            words = Counter(w.lower() for w in re.findall(r"[A-Za-z]{4,}", joined))
            terms = [t for t, _ in (grams + words).most_common(6)]
            concepts.append({
                "title": title, "explanation": joined[:220], "examples": [], "key_terms": terms,
                "diagnostic": {"prompt": f"（模擬出題）請用自己的話說明「{title}」這段在講什麼，並說明理由。",
                               "rubric": f"回答需涵蓋原文重點，提到：{'、'.join(terms)}。",
                               "hints": ["回頭看這段的第一句，它想說明什麼？", f"試著用到這些詞：{'、'.join(terms[:3])}"]},
                "transfer": {"prompt": f"（模擬出題）請舉一個教材裡沒有的例子，來說明「{title}」。",
                             "rubric": f"例子需符合原文的概念，並提到：{'、'.join(terms)}。",
                             "hints": ["想想生活中有沒有類似的情況？", f"你的例子和這些詞有什麼關係：{'、'.join(terms[:3])}"]},
            })
        if reading_questions:
            concepts[0]["diagnostic"]["prompt"] += f"（你在導讀時問過：{reading_questions[0]}）"
        return {"title": concepts[0]["title"], "concepts": concepts}

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "simulated": True, "detail": "示範模式：以關鍵字規則模擬模型輸出"}
