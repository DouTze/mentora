"""Opt-in reading evaluation on the fixed fixtures (evals/reading/manifest.json).

    cd backend
    .venv/bin/python scripts/eval_reading.py baseline  --backend mock
    .venv/bin/python scripts/eval_reading.py baseline  --backend ollama --model qwen3-vl:4b --stages segment,guide
    .venv/bin/python scripts/eval_reading.py candidate --backend ollama --model qwen3-vl:4b --stages segment,guide
    .venv/bin/python scripts/eval_reading.py compare   data/evals/<baseline-run> data/evals/<candidate-run>

baseline  = the legacy pipeline (rule atoms → model grouping → one guide call per quote; session.py behaviour).
candidate = the LangGraph pipeline (source blocks → planned units → guided-reading subgraph).
compare   = metrics side by side + a blind A/B review packet (blind/packet.md, key.json, scores.template.json);
            pass --scores <filled scores.json> to add the human rubric averages.

Each run writes <out>/results.json and <out>/report.md (default out: data/evals/<timestamp>-<mode>-<backend>,
which is git-ignored). Real backends only run when asked; nothing is downloaded and nothing leaves the machine."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.llm.base import LLMBackend, LLMError, LLMResult  # noqa: E402
from app.llm.registry import Registry, build_backend  # noqa: E402
from app.materials import MaterialError  # noqa: E402
from app.models import ModelConfig  # noqa: E402
from evals import reading_eval as ev  # noqa: E402


class CountingBackend(LLMBackend):
    """Records every generate() call (task, latency, usage, error) without changing the request."""

    def __init__(self, inner: LLMBackend):
        self.inner = inner
        self.name, self.model, self.simulated = inner.name, inner.model, inner.simulated
        self.calls: list[dict] = []

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None) -> LLMResult:
        rec = {"task": (meta or {}).get("task"), "prompt_chars": sum(len(m.get("content", "")) for m in messages)}
        t0 = time.perf_counter()
        try:
            r = await self.inner.generate(messages, temperature=temperature, max_tokens=max_tokens,
                                          context_tokens=context_tokens, meta=meta)
        except LLMError as e:
            rec.update(error=str(e), latency_ms=int((time.perf_counter() - t0) * 1000))
            self.calls.append(rec)
            raise
        rec.update(latency_ms=r.latency_ms, usage=r.usage)
        self.calls.append(rec)
        return r

    async def health(self):
        return await self.inner.health()

    async def capabilities(self):   # the wrapped adapter's real limits and vision support
        return await self.inner.capabilities()

    def count_tokens(self, messages):
        return self.inner.count_tokens(messages)

    def take(self) -> list[dict]:
        out, self.calls = self.calls, []
        return out


def prompt_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(config.PROMPT_DIR.glob("*.md")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def model_config(args) -> ModelConfig:
    return ModelConfig(backend=args.backend, model=args.model or config.OLLAMA_MODEL, host=args.host or config.OLLAMA_HOST,
                       model_path=args.model_path or config.OPENVINO_MODEL_PATH, device=args.device or config.OPENVINO_DEVICE,
                       temperature=args.temperature, max_tokens=512, context_tokens=8192)


def range_label(unit: str, n: int) -> str:
    return f"第 1–{n} {'頁' if unit == 'page' else '行'}"


# -- legacy pipeline ------------------------------------------------------------------

async def run_legacy_case(case: dict, registry: Registry, counter: CountingBackend, stages: list[str], tmp: Path) -> dict:
    from app.tutor import reading
    from app.tutor.reading_agent import InProcessReadingAgent, ReadingAgentError, ReadingWorkspace

    unit, units = ev.load_units(case)
    agent = InProcessReadingAgent(registry)
    r: dict = {"id": case["id"], "fallback_notes": [], "calls": [], "guide_latency_ms": []}
    t0 = time.perf_counter()
    atoms = reading.split_atoms(units, unit, 1)
    groups = atoms
    if "segment" in stages:
        groups = []
        for a, b in reading.plan_segments(atoms, case.get("window_chars")):
            chunk = atoms[a:b]
            try:
                groups += reading.apply_segmentation(chunk, await agent.segment(case["id"], chunk))
            except ReadingAgentError as e:
                r["fallback_notes"].append(f"候選段落 {a + 1}–{b} 改用規則切法：{e.code}: {e.detail[:120]}")
                groups += chunk
    quotes = reading.finish_quotes(unit, groups)
    parts = reading.plan_parts(quotes)
    r["segment_ms"] = int((time.perf_counter() - t0) * 1000)
    segments = [{"text": q["text"], "heading": q.get("heading")} for q in quotes]
    rep = ev.check_segmentation(case, unit, units, segments)
    r["unit_count"] = len(quotes)
    r["segmentation"] = rep.as_dict()
    r["units"] = [{"label": q["label"], "heading": q.get("heading"), "text": q["text"]} for q in quotes]
    r["calls"] += counter.take()
    if "guide" in stages:
        content = {"source": {"material_title": case["id"], "label": range_label(unit, len(units))}, "quotes": quotes, "parts": parts}
        ws = ReadingWorkspace(tmp, case["id"])
        ws.init(content)
        guides: list[dict | None] = []
        state_guides: dict[str, dict] = {}
        for q in quotes:
            prev = [p for i in range(max(0, q["index"] - 3), q["index"]) for p in state_guides.get(str(i), {}).get("key_points", [])][-6:]
            g0 = time.perf_counter()
            try:
                g = await agent.guide(ws, content, q, prev)
                state_guides[str(q["index"])] = g
                guides.append({k: g.get(k) for k in ("chinese_text", "guide", "key_points", "check")})
            except ReadingAgentError as e:
                guides.append(None)
                r.setdefault("guide_errors", []).append(f"{q['label']}: {e.code}: {e.detail[:160]}")
            ms = int((time.perf_counter() - g0) * 1000)
            r["guide_latency_ms"].append(ms)
            if q["index"] == 0:
                r["first_guide_ms"] = r["segment_ms"] + ms
        calls = counter.take()
        r["retries"] = max(0, sum(1 for c in calls if c["task"] == "guide") - len(quotes))
        r["calls"] += calls
        r["guides"] = guides
        r["guides_check"] = ev.check_guides(case, rep, segments, guides)
        attach_unit_guides(r, case, rep, segments, guides)
    if case.get("flow"):
        upto = case["flow"]["read_units"] - 1
        read = quotes[:upto + 1]
        text, _ = reading.quiz_source_text("\n\n".join(q["text"] for q in read), [p for p in parts if p["quote_from"] <= upto], {})
        r["quiz_scope"] = ev.check_quiz_scope(case, text)
    return r


def attach_unit_guides(r: dict, case: dict, rep, segments: list[dict], guides: list[dict | None]) -> None:
    """Per gold unit: the source span and the concatenated guide(s) covering it (for the blind packet)."""
    r["unit_guides"], r["unit_source"] = {}, {}
    for u in case.get("units", []):
        idx = ev.gold_unit_segments(rep, u["id"]) if u["id"] in rep.gold_spans else []
        gs = [guides[i] for i in idx if i < len(guides)]
        r["unit_source"][u["id"]] = "\n\n".join(segments[i]["text"] for i in idx)
        r["unit_guides"][u["id"]] = None if not gs or any(g is None for g in gs) else {
            "chinese_text": "\n\n".join(g["chinese_text"] for g in gs if g.get("chinese_text")) or None,
            "guide": "\n\n".join(g["guide"] for g in gs)}


# -- candidate pipeline -----------------------------------------------------------------

async def run_candidate_case(case: dict, cfg: ModelConfig, counter: CountingBackend, stages: list[str], tmp: Path) -> dict:
    try:
        from app.reading.evaluation import run_case  # added with the new pipeline (P2/P3)
    except ImportError as e:
        raise SystemExit(f"candidate pipeline is not available yet: {e}") from e
    return await run_case(case, cfg, counter, stages, tmp, ev=ev, attach_unit_guides=attach_unit_guides)


# -- driver --------------------------------------------------------------------------

async def run(args) -> Path:
    cfg = model_config(args)
    counter = CountingBackend(build_backend(cfg))
    registry = Registry(cfg)
    registry._backend = counter  # the legacy agent reads registry.backend; counting wraps the real adapter
    stages = args.stages.split(",")
    manifest = ev.load_manifest()
    cases = ev.select_cases(manifest, args.cases.split(",") if args.cases else None)
    out = Path(args.out) if args.out else ROOT / "data" / "evals" / f"{datetime.now():%Y%m%d-%H%M%S}-{args.mode}-{cfg.backend}"
    out.mkdir(parents=True, exist_ok=True)
    started, t0 = datetime.now().isoformat(timespec="seconds"), time.perf_counter()
    results = []
    with tempfile.TemporaryDirectory() as td:
        for case in cases:
            print(f"[{args.mode}] {case['id']} …", flush=True)
            if "expect_import_error" in case:
                try:
                    ev.load_units(case)
                    res = {"id": case["id"], "import_error": None}
                except MaterialError as e:
                    res = {"id": case["id"], "import_error": e.code}
                res["expect_import_error"] = case["expect_import_error"]
                res["import_error_ok"] = res["import_error"] == case["expect_import_error"]
                results.append(res)
                continue
            try:
                if args.mode == "baseline":
                    res = await run_legacy_case(case, registry, counter, stages, Path(td))
                else:
                    res = await run_candidate_case(case, cfg, counter, stages, Path(td))
            except LLMError as e:
                res = {"id": case["id"], "error": f"model_unavailable: {e}", "calls": counter.take()}
            results.append(res)
            (out / "results.partial.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    run_doc = {"mode": args.mode, "pipeline": "legacy" if args.mode == "baseline" else "langgraph",
               "started_at": started, "elapsed_s": round(time.perf_counter() - t0, 1),
               "backend": {**cfg.model_dump(), "resolved_model": counter.model, "simulated": counter.simulated},
               "stages": stages, "prompt_hash": prompt_hash(), "manifest_hash": ev.file_hash(ev.MANIFEST),
               "results": results, "summary": ev.summarize(results)}
    (out / "results.json").write_text(json.dumps(run_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "results.partial.json").unlink(missing_ok=True)
    (out / "report.md").write_text(ev.markdown_report(run_doc), encoding="utf-8")
    print(json.dumps(run_doc["summary"], ensure_ascii=False, indent=1))
    print(f"→ {out}")
    return out


def compare(args) -> Path:
    a = json.loads((Path(args.a) / "results.json").read_text(encoding="utf-8"))
    b = json.loads((Path(args.b) / "results.json").read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else ROOT / "data" / "evals" / f"{datetime.now():%Y%m%d-%H%M%S}-compare"
    (out / "blind").mkdir(parents=True, exist_ok=True)
    keys = sorted(set(a["summary"]) | set(b["summary"]))
    lines = ["# 導讀評估比較", "", f"- A：{args.a}（{a['mode']}，{a['backend'].get('resolved_model')}）",
             f"- B：{args.b}（{b['mode']}，{b['backend'].get('resolved_model')}）", "",
             "| 指標 | A | B |", "| --- | --- | --- |"]
    for k in keys:
        fa, fb = (json.dumps(x.get(k), ensure_ascii=False) for x in (a["summary"], b["summary"]))
        lines.append(f"| {k} | {fa} | {fb} |")
    packet, key, template = ev.blind_packet(a, b, label_a=Path(args.a).name, label_b=Path(args.b).name)
    (out / "blind" / "packet.md").write_text(packet, encoding="utf-8")
    (out / "blind" / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "blind" / "scores.template.json").write_text(json.dumps(template, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.scores:
        k = key if not args.key else json.loads(Path(args.key).read_text())
        sc = json.loads(Path(args.scores).read_text())
        human = {"paired": ev.score_blind(k, sc), "held_review": ev.score_held(k, sc)}
        lines += ["", "## 人工 rubric（盲評）", "", "```json", json.dumps(human, ensure_ascii=False, indent=1), "```"]
    lines += ["", f"盲評包：`{out / 'blind' / 'packet.md'}`（{len(key)} 題）。評分後以 `--scores` 重新執行 compare。"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    for mode in ("baseline", "candidate"):
        p = sub.add_parser(mode)
        p.add_argument("--backend", choices=["mock", "ollama", "openvino"], default="mock")
        p.add_argument("--model", default="")
        p.add_argument("--host", default="")
        p.add_argument("--model-path", default="")
        p.add_argument("--device", default="")
        p.add_argument("--temperature", type=float, default=0.2)
        p.add_argument("--stages", default="segment,guide", help="comma list: segment, guide")
        p.add_argument("--cases", default="", help="comma list of case ids (default: all)")
        p.add_argument("--out", default="")
    p = sub.add_parser("compare")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--scores", default="")
    p.add_argument("--key", default="")
    p.add_argument("--out", default="")
    args = ap.parse_args()
    if args.mode == "compare":
        compare(args)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
