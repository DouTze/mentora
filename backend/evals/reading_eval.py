"""Reading evaluation: fixed fixtures (evals/reading/manifest.json), automatic checks and report helpers.

Everything that decides pass/fail is here and pure, so tests can pin the contract; scripts/eval_reading.py only
runs pipelines and writes files. Gold labels are anchors (verbatim snippets of the source), never one model's
segmentation, so several valid cuts can pass:

- a gold unit starts at its `start` anchor; `merge_ok` means it may be merged into the previous unit;
- `contains` anchors must land in the same candidate unit as the gold unit's start;
- `allowed_breaks` anchors (and, with `sentence_breaks_ok`, any sentence end) are acceptable extra boundaries;
- a boundary inside $…$, $$…$$ or a code fence is always a violation.

The automatic guide checks (must_keep keywords, forbidden text, external narration, chinese_text) are signals for
the human rubric in evals/reading/RUBRIC.md, not a replacement for it."""
from __future__ import annotations

import hashlib
import json
import random
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent / "reading"
MANIFEST = EVAL_DIR / "manifest.json"

WS_RE = re.compile(r"\s+")
SENTENCE_END = set("。！？.!?；;")
NARRATION = ("原文指出", "作者在這裡", "作者說", "請參考原文", "回去看原文", "本段想表達", "這一段的重點是", "課文接著", "如上所述")
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")


def norm(s: str) -> str:
    """Whitespace-free form used for all matching (PDF / line joins differ only in whitespace)."""
    return WS_RE.sub("", s or "")


def is_chinese(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and len(CJK_RE.findall(text)) / len(letters) >= 0.3


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# -- fixtures -----------------------------------------------------------------

def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def select_cases(manifest: dict, ids: list[str] | None = None, *, include_broken: bool = True) -> list[dict]:
    cases = manifest["cases"]
    if ids:
        known = {c["id"] for c in cases}
        missing = [i for i in ids if i not in known]
        if missing:
            raise KeyError(f"unknown case ids: {missing}")
        cases = [c for c in cases if c["id"] in ids]
    if not include_broken:
        cases = [c for c in cases if "expect_import_error" not in c]
    return cases


def load_units(case: dict, base: Path = EVAL_DIR) -> tuple[str, list[str]]:
    """(unit kind, units) exactly as the importer would produce them; `pages` fixtures are already-extracted
    PDF pages (so the test does not depend on PyMuPDF layout heuristics)."""
    from app.materials import KINDS, MaterialError, extract_units

    path = base / case["source"]
    if case["kind"] == "pages":
        return "page", json.loads(path.read_text(encoding="utf-8"))
    kind = KINDS.get(path.suffix.lower())
    if not kind:
        raise MaterialError("unsupported", "目前只支援 pdf、md、txt。")
    return ("page" if kind == "pdf" else "line"), extract_units(kind, path.read_bytes())


def split_body(unit: str, units: list[str]) -> tuple[str, list[str]]:
    """(normalised body text without heading lines, normalised headings). Pipelines move headings into a
    `heading` field, so body text is what units must cover exactly once and in order."""
    body, heads = [], []
    if unit == "line":
        fence = False
        for ln in units:
            if ln.lstrip().startswith("```"):
                fence = not fence
            m = None if fence else MD_HEADING_RE.match(ln)
            (heads if m else body).append(norm(m.group(1)) if m else ln)
    else:
        from app.reading.source import pdf_layout   # same heading guess as the extractor

        for page, kinds in zip(units, pdf_layout(units)):
            for a, b, kind in kinds:
                (heads if kind == "heading" else body).append(norm(page[a:b]) if kind == "heading" else page[a:b])
    return norm("\n".join(body)), [h for h in heads if h]


# -- segmentation checks --------------------------------------------------------

@dataclass
class SegReport:
    integrity_ok: bool
    missing_text: list[str] = field(default_factory=list)      # source text no unit covers (beyond headings)
    order_errors: list[str] = field(default_factory=list)      # unit text not found in order (dup / reorder / rewrite)
    headings_lost: list[str] = field(default_factory=list)     # source headings no unit shows (warning, not integrity)
    missing_boundaries: list[str] = field(default_factory=list)
    extra_boundaries: list[str] = field(default_factory=list)
    together_violations: list[str] = field(default_factory=list)
    protected_violations: list[str] = field(default_factory=list)
    unit_spans: list[tuple[int, int]] = field(default_factory=list)   # candidate unit [start, end) in source_norm
    gold_spans: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def boundary_ok(self) -> bool:
        return not (self.missing_boundaries or self.extra_boundaries or self.together_violations or self.protected_violations)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("unit_spans", "gold_spans")}
        d["boundary_ok"] = self.boundary_ok
        return d


def _protected_ranges(src: str) -> list[tuple[int, int]]:
    """[start, end) ranges of $$…$$, ```…``` and inline $…$ in normalised source text."""
    ranges = []
    for pat in (r"```.*?```", r"\$\$.*?\$\$"):
        ranges += [(m.start(), m.end()) for m in re.finditer(pat, src, re.S)]
    covered = lambda i: any(a <= i < b for a, b in ranges)  # noqa: E731
    i, inline = 0, []
    while i < len(src):
        if src[i] == "$" and not covered(i):
            j = src.find("$", i + 1)
            while j != -1 and covered(j):
                j = src.find("$", j + 1)
            if j == -1:
                break
            inline.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return ranges + inline


def _locate(src: str, anchors: list[str]) -> list[int]:
    """Sequential search: duplicated text is matched to the occurrence after the previous anchor."""
    pos, out = 0, []
    for a in anchors:
        i = src.find(norm(a), pos)
        if i == -1:
            i = src.find(norm(a))  # out-of-order anchor: still report, never crash
        out.append(i)
        if i >= 0:
            pos = i + 1
    return out


def check_segmentation(case: dict, unit: str, units: list[str], segments: list[dict]) -> SegReport:
    """segments: [{"text": body text without heading lines, "heading": optional}] in reading order."""
    src, headings = split_body(unit, units)
    spans: list[tuple[int, int]] = []
    order_errors: list[str] = []
    pos = 0
    heading_set = set(headings)
    for i, s in enumerate(segments):
        # a heading the planner kept inside a unit is a line of its text, not body text of the source
        t = norm("\n".join(ln for ln in s["text"].split("\n") if norm(ln) not in heading_set))
        if not t:
            order_errors.append(f"unit {i + 1} is empty")
            continue
        j = src.find(t, pos)
        if j == -1:
            order_errors.append(f"unit {i + 1} text not found in source order: {s['text'][:30]!r}")
            continue
        spans.append((j, j + len(t)))
        pos = j + len(t)
    # uncovered source text must be only heading text / markup
    missing: list[str] = []
    cursor = 0
    gaps = []
    for a, b in spans:
        if a > cursor:
            gaps.append(src[cursor:a])
        cursor = max(cursor, b)
    if cursor < len(src):
        gaps.append(src[cursor:])
    missing = [g[:60] for g in gaps if g]
    shown = norm(" ".join(x for s in segments for x in re.split(r"\s*›\s*", s.get("heading") or "")))
    lost = [h for h in headings if h not in shown]
    rep = SegReport(integrity_ok=not missing and not order_errors, missing_text=missing, order_errors=order_errors,
                    headings_lost=lost, unit_spans=spans)

    gold = case.get("units", [])
    starts = _locate(src, [u["start"] for u in gold])
    for u, a in zip(gold, starts):
        if a < 0:
            raise ValueError(f"{case['id']}: gold anchor not in source: {u['start']!r}")
    for k, u in enumerate(gold):
        end = starts[k + 1] if k + 1 < len(gold) else len(src)
        rep.gold_spans[u["id"]] = (starts[k], end)
    cand = {a for a, _ in spans}
    allowed = set(starts) | {i for i in _locate(src, case.get("allowed_breaks", [])) if i >= 0}
    protected = _protected_ranges(src)
    for u, a in zip(gold, starts):
        if a not in cand and not u.get("merge_ok") and spans:
            rep.missing_boundaries.append(f"{u['id']} 應從「{u['start']}」開始新單元")
    first = spans[0][0] if spans else 0
    for c in sorted(cand):
        if c == first:
            continue
        inside = [(a, b) for a, b in protected if a < c < b]
        if inside:
            rep.protected_violations.append(f"在公式／程式碼中切開：…{src[max(0, c - 12):c]}|{src[c:c + 12]}…")
            continue
        if c in allowed:
            continue
        if case.get("sentence_breaks_ok") and c > 0 and src[c - 1] in SENTENCE_END:
            continue
        rep.extra_boundaries.append(f"不在允許的邊界：…{src[max(0, c - 12):c]}|{src[c:c + 12]}…")
        for u, a in zip(gold, starts):
            for anchor in u.get("contains", []):
                b = src.find(norm(anchor), a)
                if a < c <= b:
                    rep.together_violations.append(f"{u['id']}：「{u['start'][:12]}」與「{anchor[:12]}」被切開")
    return rep


def gold_unit_segments(rep: SegReport, gold_id: str) -> list[int]:
    """Candidate unit indices overlapping a gold unit's span (used to check that unit's guide content)."""
    a, b = rep.gold_spans[gold_id]
    return [i for i, (x, y) in enumerate(rep.unit_spans) if x < b and y > a]


# -- guide checks ---------------------------------------------------------------

def _has(text_norm: str, words: list[str]) -> bool:
    low = text_norm.lower()
    return all(norm(w).lower() in low for w in words)


def check_guides(case: dict, rep: SegReport, segments: list[dict], guides: list[dict | None]) -> dict:
    """Per gold unit: which must_keep items the guide text (guide + chinese_text) covers, forbidden hits,
    narration phrases, missing chinese_text for non-Chinese source. guides[i] belongs to segments[i]."""
    out: dict[str, Any] = {"units": {}, "kept": 0, "total_keep": 0, "forbidden_hits": 0, "narration_hits": 0,
                           "missing_guides": 0, "missing_chinese_text": 0, "kept_guide_only": 0}
    case_forbidden = case.get("forbidden", [])
    for u in case.get("units", []):
        idx = gold_unit_segments(rep, u["id"]) if u["id"] in rep.gold_spans else []
        gs = [guides[i] for i in idx if i < len(guides)]
        if not gs or any(g is None for g in gs):
            out["missing_guides"] += 1
            out["units"][u["id"]] = {"guide": None, "segments": idx}
            out["total_keep"] += len(u.get("must_keep", []))
            continue
        body = "\n".join((g.get("guide") or "") + "\n" + (g.get("chinese_text") or "") for g in gs)
        bn = norm(body)
        kept, lost = [], []
        for item in u.get("must_keep", []):
            (kept if any(_has(bn, grp) for grp in item["any"]) else lost).append(item["text"])
        gn = norm("\n".join(g.get("guide") or "" for g in gs))   # the 主要正文 alone (it must not rely on the 中文課文)
        kept_guide = [item["text"] for item in u.get("must_keep", []) if any(_has(gn, grp) for grp in item["any"])]
        forbidden = [f["text"] for f in case_forbidden + u.get("forbidden", []) if any(norm(x) in bn for x in f["any"])]
        exact = [x for x in u.get("forbidden_exact", []) if any((g.get("guide") or "").strip() == x for g in gs)]
        narration = [p for p in NARRATION if p in body]
        no_zh = [i for i in idx if not is_chinese(segments[i]["text"]) and not (guides[i] or {}).get("chinese_text")]
        out["units"][u["id"]] = {"segments": idx, "kept": kept, "lost": lost, "forbidden": forbidden + exact,
                                 "narration": narration, "missing_chinese_text": bool(no_zh), "kept_guide_only": kept_guide}
        out["kept"] += len(kept)
        out["kept_guide_only"] += len(kept_guide)
        out["total_keep"] += len(kept) + len(lost)
        out["forbidden_hits"] += len(forbidden) + len(exact)
        out["narration_hits"] += len(narration)
        out["missing_chinese_text"] += int(bool(no_zh))
    return out


def check_quiz_scope(case: dict, scope_text: str) -> dict | None:
    flow = case.get("flow")
    if not flow:
        return None
    t = norm(scope_text)
    inc = [a for a in flow.get("quiz_scope_must_include", []) if norm(a) not in t]
    exc = [a for a in flow.get("quiz_scope_must_exclude", []) if norm(a) in t]
    return {"ok": not inc and not exc, "missing": inc, "leaked": exc}


# -- reporting ------------------------------------------------------------------

def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def summarize(results: list[dict]) -> dict:
    """Aggregate per-case results (as written by scripts/eval_reading.py) into the acceptance metrics."""
    seg = [r for r in results if r.get("segmentation")]
    guide = [r for r in results if r.get("guides_check")]
    unit_ms = [ms for r in results for ms in r.get("guide_latency_ms", [])]
    first_ms = [r["first_guide_ms"] for r in results if r.get("first_guide_ms") is not None]
    calls = [c for r in results for c in r.get("calls", [])]
    s = {
        "cases": len(results),
        "import_errors_as_expected": sum(1 for r in results if r.get("import_error_ok")),
        "integrity_ok": sum(1 for r in seg if r["segmentation"]["integrity_ok"]),
        "boundary_ok": sum(1 for r in seg if r["segmentation"]["boundary_ok"]),
        "segmented_cases": len(seg),
        "missing_boundaries": sum(len(r["segmentation"]["missing_boundaries"]) for r in seg),
        "extra_boundaries": sum(len(r["segmentation"]["extra_boundaries"]) for r in seg),
        "together_violations": sum(len(r["segmentation"]["together_violations"]) for r in seg),
        "protected_violations": sum(len(r["segmentation"]["protected_violations"]) for r in seg),
        "headings_lost": sum(len(r["segmentation"].get("headings_lost", [])) for r in seg),
        "keep_ratio": None, "forbidden_hits": 0, "narration_hits": 0, "missing_guides": 0, "missing_chinese_text": 0,
        "quiz_scope_ok": sum(1 for r in results if (r.get("quiz_scope") or {}).get("ok")),
        "quiz_scope_cases": sum(1 for r in results if r.get("quiz_scope") is not None),
        "first_guide_ms": {"p50": percentile(first_ms, .5), "max": max(first_ms) if first_ms else None},
        "guide_ms": {"n": len(unit_ms), "p50": percentile(unit_ms, .5), "p95": percentile(unit_ms, .95)},
        "model_calls": len(calls),
        "model_errors": sum(1 for c in calls if c.get("error")),
        "prompt_tokens": sum((c.get("usage") or {}).get("prompt_tokens") or 0 for c in calls),
        "completion_tokens": sum((c.get("usage") or {}).get("completion_tokens") or 0 for c in calls),
        "retries": sum(r.get("retries", 0) for r in results),
        "fallbacks": sum(len(r.get("fallback_notes", [])) for r in results),
        "cache_hits": sum(r.get("cache_hits", 0) for r in results),
        "budget_exceeded": sum(1 for r in results if r.get("budget_exceeded")),
    }
    tk = sum(r["guides_check"]["total_keep"] for r in guide)
    if tk:
        s["keep_ratio"] = round(sum(r["guides_check"]["kept"] for r in guide) / tk, 3)
        s["keep_ratio_guide_only"] = round(sum(r["guides_check"].get("kept_guide_only", 0) for r in guide) / tk, 3)
    for key in ("forbidden_hits", "narration_hits", "missing_guides", "missing_chinese_text"):
        s[key] = sum(r["guides_check"][key] for r in guide)
    calls_by_task: dict[str, int] = {}
    for c in calls:
        calls_by_task[c.get("task") or "?"] = calls_by_task.get(c.get("task") or "?", 0) + 1
    s["calls_by_task"] = calls_by_task
    return s


def provenance_lines(prov: dict | None) -> list[str]:
    if not prov:
        return []
    lines = [f"- 執行類別：{prov.get('run_class')}　主機：{prov.get('host')}　模型 digest：`{prov.get('model_digest')}`"
             f"　seed：{prov.get('seed')}　平行：{prov.get('jobs')}"]
    if prov.get("remote"):
        if prov.get("digest_match"):
            lines.append("- **遠端推論**：模型 digest 與本機相同，切段、導讀、關卡與盲評結果可作為品質驗收；耗時不能當作驗收。")
        else:
            lines.append(f"- **遠端推論，模型未驗證**：遠端 digest 無法和本機（`{prov.get('local_model_digest')}`）比對，"
                         "結果只供開發參考，不能驗收。")
    elif not prov.get("latency_comparable", True):
        lines.append("- 多份教材平行執行：品質結果可用；耗時不能和循序的本機驗收比較。")
    return lines


def markdown_report(run: dict) -> str:
    s = run["summary"]
    lines = [f"# 導讀評估：{run['mode']}", "",
             f"- 時間：{run['started_at']}　耗時 {run['elapsed_s']} 秒",
             f"- 後端：{run['backend']}　prompt bundle：`{run['prompt_hash']}`　manifest：`{run['manifest_hash']}`",
             f"- pipeline：{run['pipeline']}　stages：{', '.join(run['stages'])}", *provenance_lines(run.get("provenance")), "",
             "## 摘要", "", "| 指標 | 值 |", "| --- | --- |"]
    for k, v in s.items():
        lines.append(f"| {k} | {json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v} |")
    lines += ["", "## 逐例", ""]
    for r in run["results"]:
        lines.append(f"### {r['id']}")
        if "import_error" in r:
            lines.append(f"- 匯入錯誤：`{r['import_error']}`（預期 `{r.get('expect_import_error')}`）→ {'符合' if r.get('import_error_ok') else '**不符**'}")
            lines.append("")
            continue
        if r.get("error"):
            lines.append(f"- **錯誤**：{r['error']}")
        sg = r.get("segmentation")
        if sg:
            lines.append(f"- 單元數 {r['unit_count']}；完整性 {'✓' if sg['integrity_ok'] else '✗'}；邊界 {'✓' if sg['boundary_ok'] else '✗'}")
            for key in ("missing_text", "order_errors", "headings_lost", "missing_boundaries", "extra_boundaries", "together_violations", "protected_violations"):
                for x in sg[key]:
                    lines.append(f"  - {key}: {x}")
        for n in r.get("fallback_notes", []):
            lines.append(f"- fallback：{n}")
        gc = r.get("guides_check")
        if gc:
            lines.append(f"- 保留 {gc['kept']}/{gc['total_keep']}；forbidden {gc['forbidden_hits']}；旁白 {gc['narration_hits']}；缺導讀 {gc['missing_guides']}")
            for uid, u in gc["units"].items():
                if u.get("lost") or u.get("forbidden") or u.get("narration") or u.get("guide", 1) is None:
                    lines.append(f"  - {uid}: lost={u.get('lost')} forbidden={u.get('forbidden')} narration={u.get('narration')}")
        if r.get("quiz_scope"):
            lines.append(f"- 出題範圍：{r['quiz_scope']}")
        if r.get("guide_latency_ms"):
            lines.append(f"- 每段導讀耗時（ms）：{r['guide_latency_ms']}")
        lines.append("")
    return "\n".join(lines) + "\n"


# -- blind review -----------------------------------------------------------------

GUIDE_FIELDS = {"faithfulness": None, "completeness": None, "clarity": None, "wrong_formula": False,
                "fabricated_claim": False, "missing_key_condition": False, "supplement_incorrect": False,
                "supplement_unnecessary": False, "note": ""}
CN_FIELDS = {"faithfulness": None, "completeness": None, "wrong_formula": False, "fabricated_claim": False,
             "missing_key_condition": False, "note": ""}


HELD_FIELDS = {"draft_acceptable": None, "has_major_error": None, "note": ""}


def run_labels(a: dict, b: dict, label_a: str | None, label_b: str | None) -> tuple[str, str]:
    """Distinct run identifiers for the key: two candidate runs (before / after a change) must never be merged."""
    la, lb = label_a or a.get("run_id") or a["mode"], label_b or b.get("run_id") or b["mode"]
    return (la, lb) if la != lb else (f"{la}#A", f"{lb}#B")


def _unit_id(block_ids: list[str]) -> str:
    from app.llm.runtime import stable_hash
    return f"unit-{stable_hash([block_ids[0], block_ids[-1]], 10)}"


def held_items(run: dict) -> list[dict]:
    """Units a graph run did not publish, with the last draft it held back (stored, or recovered from the raw trace
    of older result files) — reviewed to measure correct drafts that were wrongly blocked."""
    out = []
    for r in run.get("results", []):
        units = r.get("units") or []
        for k, s in enumerate(r.get("guide_status") or []):
            if s["status"] == "accepted":
                continue
            unit = units[k] if k < len(units) else {}
            draft = s.get("held_draft")
            if draft is None and unit.get("block_ids"):
                uid = s.get("unit_id") or _unit_id(unit["block_ids"])
                for t in r.get("llm_trace", []):
                    if t["node"] in ("draft", "revise_body") and f":{uid}:" in (t.get("operation_key") or "") and t.get("raw"):
                        try:
                            draft = json.loads(t["raw"]).get("guide") or draft
                        except ValueError:
                            pass
            out.append({"case": r["id"], "unit": s.get("unit"), "source": unit.get("text", ""), "draft": draft,
                        "reason": (s.get("error") or {}).get("code"), "detail": (s.get("error") or {}).get("detail", "")})
    return out


def blind_packet(a: dict, b: dict, seed: int = 7, label_a: str | None = None,
                 label_b: str | None = None) -> tuple[str, dict, dict]:
    """Pair the outputs of two runs per gold unit in random A/B order. Returns (markdown packet, key, score template).
    The 主要正文 is scored on its own first (it must teach the unit without the 中文課文); the 中文課文, when there
    is one, is shown afterwards and scored separately. Runs are keyed by a run id (never by mode: two candidate
    runs stay apart). Drafts a run held back are listed afterwards for a separate review (wrongly blocked?).
    Scores follow RUBRIC.md."""
    rng = random.Random(seed)
    la, lb = run_labels(a, b, label_a, label_b)
    by_id = {r["id"]: r for r in b["results"]}
    lines = ["# 導讀盲評包", "", "每一題比較兩份導讀（X／Y 順序隨機）。先只讀兩份**主要正文**評分（不要先看中文課文），"
             "再看各自的中文課文另外評分。依 evals/reading/RUBRIC.md 評分，填入 scores.json。", ""]
    key: dict[str, dict] = {}
    template: dict[str, dict] = {}
    for ra in a["results"]:
        rb = by_id.get(ra["id"])
        if not rb or not ra.get("unit_guides") or not rb.get("unit_guides"):
            continue
        for uid, ga in ra["unit_guides"].items():
            gb = rb["unit_guides"].get(uid)
            if not ga or not gb:
                continue
            item = f"{ra['id']}/{uid}"
            flip = rng.random() < 0.5
            x, y = (gb, ga) if flip else (ga, gb)
            key[item] = {"X": lb if flip else la, "Y": la if flip else lb}
            has_cn = bool(x.get("chinese_text") or y.get("chinese_text"))
            template[item] = {side: {"guide": dict(GUIDE_FIELDS), **({"chinese_text": dict(CN_FIELDS)} if has_cn else {})}
                              for side in ("X", "Y")}
            lines += [f"## {item}", "", "**原文**", "", "```text", ra["unit_source"][uid], "```", ""]
            for side, g in (("X", x), ("Y", y)):
                lines += [f"### {side}：主要正文", "", g.get("guide") or "", ""]
            if has_cn:
                for side, g in (("X", x), ("Y", y)):
                    lines += [f"### {side}：中文課文（另評）", "", g.get("chinese_text") or "（沒有）", ""]
    held = [(lbl, h) for lbl, run in ((la, a), (lb, b)) for h in held_items(run) if h["draft"]]
    if held:
        lines += ["# 未公開草稿審查", "", "以下草稿被流程擋下沒有公開（不標示來自哪一份輸出）。只看原文與草稿，判斷：草稿其實可以公開嗎"
                  "（draft_acceptable）？草稿有重大錯誤嗎（has_major_error）？", ""]
        rng.shuffle(held)
        for k, (lbl, h) in enumerate(held, 1):
            item = f"held/{k:02d}"
            key[item] = {"run": lbl, "kind": "held", "reason": h["reason"]}
            template[item] = dict(HELD_FIELDS)
            lines += [f"## {item}", "", "**原文**", "", "```text", h["source"], "```", "", "**被擋下的主要正文**", "", h["draft"], ""]
    return "\n".join(lines) + "\n", key, template


def score_blind(key: dict, scores: dict) -> dict:
    """Average human rubric scores per pipeline, the 主要正文 and the 中文課文 separately; counts of major errors
    (must be 0 to pass). Accepts the older flat score shape (one block per side) as 主要正文 scores."""
    majors = ("wrong_formula", "fabricated_claim", "missing_key_condition")
    agg: dict[str, dict[str, dict[str, list]]] = {}
    for item, sides in scores.items():
        if key.get(item, {}).get("kind") == "held":
            continue
        for side, sc in sides.items():
            mode = key[item][side]
            parts = sc if "guide" in sc else {"guide": sc}
            for part, vals in parts.items():
                a = agg.setdefault(mode, {}).setdefault(part, {"faithfulness": [], "completeness": [], "clarity": [],
                                                              "major": [], "supplement_issues": []})
                for dim in ("faithfulness", "completeness", "clarity"):
                    if vals.get(dim) is not None:
                        a[dim].append(vals[dim])
                a["major"].append(int(any(vals.get(m) for m in majors)))
                a["supplement_issues"].append(int(bool(vals.get("supplement_incorrect") or vals.get("supplement_unnecessary"))))
    out = {}
    for mode, parts in agg.items():
        out[mode] = {}
        for part, a in parts.items():
            out[mode][part] = {**{dim: round(statistics.mean(v), 2) if v else None for dim, v in a.items()
                                  if dim in ("faithfulness", "completeness", "clarity")},
                               "major_errors": sum(a["major"]), "supplement_issues": sum(a["supplement_issues"]),
                               "n": len(a["major"])}
        g = out[mode].get("guide", {})   # flat aliases of the 主要正文 scores (the acceptance gate)
        out[mode].update({k: g.get(k) for k in ("faithfulness", "completeness", "clarity", "major_errors", "n")})
    return out


def score_held(key: dict, scores: dict) -> dict:
    """Per run: held drafts reviewed, how many were acceptable (wrongly blocked) or had a major error (rightly)."""
    out: dict[str, dict] = {}
    for item, k in key.items():
        if k.get("kind") != "held" or item not in scores:
            continue
        sc = scores[item]
        o = out.setdefault(k["run"], {"reviewed": 0, "wrongly_blocked": 0, "rightly_blocked": 0, "by_reason": {}})
        if sc.get("draft_acceptable") is None:
            continue
        o["reviewed"] += 1
        o["wrongly_blocked"] += int(bool(sc["draft_acceptable"]) and not sc.get("has_major_error"))
        o["rightly_blocked"] += int(bool(sc.get("has_major_error")))
        r = o["by_reason"].setdefault(k.get("reason") or "?", {"reviewed": 0, "wrongly_blocked": 0})
        r["reviewed"] += 1
        r["wrongly_blocked"] += int(bool(sc["draft_acceptable"]) and not sc.get("has_major_error"))
    return out
