"""Opt-in reading evaluation on the fixed fixtures (evals/reading/manifest.json).

    cd backend
    .venv/bin/python scripts/eval_reading.py candidate --backend mock
    .venv/bin/python scripts/eval_reading.py candidate --backend ollama --model qwen3-vl:4b --stages segment,guide
    .venv/bin/python scripts/eval_reading.py compare   data/evals/<run-A> data/evals/<run-B>

candidate = the reading pipeline the app runs (source blocks → planned units → guided-reading subgraph).
compare   = metrics side by side + a blind A/B review packet (blind/packet.md, key.json, scores.template.json) of two
            saved runs — e.g. a new run against the results of the removed legacy pipeline kept in data/evals;
            pass --scores <filled scores.json> to add the human rubric averages.

Each run writes <out>/results.json and <out>/report.md (default out: data/evals/<timestamp>-<mode>-<backend>,
which is git-ignored). Real backends only run when asked; nothing is downloaded.

Remote inference (development only): with --host pointing at an Ollama on the internet (e.g. the Colab notebook in
scripts/colab/), the fixture text is sent to that host, so it is refused unless MENTOR_ALLOW_REMOTE_MODEL=1 is set.
The run records provenance (host, remote and local model digests, seed, jobs). When the remote digest equals the
local one it is the same model, so the quality results (segmentation, guides, gates, blind packets) count for
acceptance ("remote-quality"); otherwise it is only a development reference ("remote-unverified"). Latency counts
only for local sequential runs ("local-acceptance").
--jobs N runs N fixtures concurrently (one counter per fixture)."""
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
from app.llm.registry import build_backend  # noqa: E402
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


def provenance(args, cfg: ModelConfig, backend: LLMBackend, local_host: str = "http://127.0.0.1:11434",
               models_dir: str | None = None) -> dict:
    """Where the model ran and what the run may be accepted for: quality (same model, checked by digest) and/or
    latency (local, sequential only)."""
    from app.llm import ollama
    remote = cfg.backend == "ollama" and ollama.is_remote(cfg.host)
    digest = local_digest = None
    if cfg.backend == "ollama":
        digest = asyncio.run(backend.model_digest())
        if remote:   # the same model locally, to prove the remote one is not a different model
            local_digest = (asyncio.run(ollama.OllamaBackend(local_host, cfg.model).model_digest())
                            or ollama.manifest_digest(cfg.model, models_dir))
    jobs = getattr(args, "jobs", 1)
    digest_match = (digest is not None and digest == local_digest) if remote else None
    run_class = ("mock" if cfg.backend == "mock" else
                 ("remote-quality" if digest_match else "remote-unverified") if remote else
                 "local-parallel" if jobs > 1 else "local-acceptance")
    return {"host": cfg.host if cfg.backend == "ollama" else None, "remote": remote, "model_digest": digest,
            "local_model_digest": local_digest, "digest_match": digest_match,
            "seed": ollama.sampling_seed() if cfg.backend == "ollama" else None, "temperature": cfg.temperature,
            "jobs": jobs, "run_class": run_class,
            "quality_acceptable": cfg.backend != "mock" and digest_match is not False,
            "latency_comparable": not remote and jobs == 1}


def model_config(args) -> ModelConfig:
    return ModelConfig(backend=args.backend, model=args.model or config.OLLAMA_MODEL, host=args.host or config.OLLAMA_HOST,
                       model_path=args.model_path or config.OPENVINO_MODEL_PATH, device=args.device or config.OPENVINO_DEVICE,
                       temperature=args.temperature, max_tokens=512, context_tokens=8192)



# -- candidate pipeline -----------------------------------------------------------------

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


async def run_candidate_case(case: dict, cfg: ModelConfig, counter: CountingBackend, stages: list[str], tmp: Path) -> dict:
    try:
        from app.reading.evaluation import run_case  # added with the new pipeline (P2/P3)
    except ImportError as e:
        raise SystemExit(f"candidate pipeline is not available yet: {e}") from e
    return await run_case(case, cfg, counter, stages, tmp, ev=ev, attach_unit_guides=attach_unit_guides)


# -- driver --------------------------------------------------------------------------

async def run_one(args, case: dict, cfg: ModelConfig, counter: CountingBackend, stages: list[str],
                  tmp: Path) -> dict:
    if "expect_import_error" in case:
        try:
            ev.load_units(case)
            res = {"id": case["id"], "import_error": None}
        except MaterialError as e:
            res = {"id": case["id"], "import_error": e.code}
        res["expect_import_error"] = case["expect_import_error"]
        res["import_error_ok"] = res["import_error"] == case["expect_import_error"]
        return res
    try:
        return await run_candidate_case(case, cfg, counter, stages, tmp)
    except LLMError as e:
        return {"id": case["id"], "error": f"model_unavailable: {e}", "calls": counter.take()}


async def run(args, prov: dict | None = None) -> Path:
    cfg = model_config(args)
    inner = build_backend(cfg)
    counter = CountingBackend(inner)
    stages = args.stages.split(",")
    manifest = ev.load_manifest()
    cases = ev.select_cases(manifest, args.cases.split(",") if args.cases else None)
    out = Path(args.out) if args.out else ROOT / "data" / "evals" / f"{datetime.now():%Y%m%d-%H%M%S}-{args.mode}-{cfg.backend}"
    out.mkdir(parents=True, exist_ok=True)
    started, t0 = datetime.now().isoformat(timespec="seconds"), time.perf_counter()
    jobs = max(1, args.jobs)
    results: list[dict | None] = [None] * len(cases)

    def save_partial():
        (out / "results.partial.json").write_text(json.dumps([r for r in results if r], ensure_ascii=False, indent=1),
                                                  encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        if jobs == 1:
            for i, case in enumerate(cases):
                print(f"[{args.mode}] {case['id']} …", flush=True)
                results[i] = await run_one(args, case, cfg, counter, stages, Path(td))
                save_partial()
        else:
            gate = asyncio.Semaphore(jobs)

            async def one(i: int, case: dict):
                async with gate:   # each fixture counts its own calls; the adapter itself is shared
                    print(f"[{args.mode}] {case['id']} …", flush=True)
                    results[i] = await run_one(args, case, cfg, CountingBackend(inner), stages, Path(td))
                    save_partial()
            await asyncio.gather(*(one(i, c) for i, c in enumerate(cases)))
    results = [r for r in results if r]
    run_doc = {"mode": args.mode, "pipeline": "langgraph",
               "started_at": started, "elapsed_s": round(time.perf_counter() - t0, 1),
               "backend": {**cfg.model_dump(), "resolved_model": counter.model, "simulated": counter.simulated},
               "provenance": prov or {},
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
    for mode in ("candidate",):
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
        p.add_argument("--jobs", type=int, default=1, help="fixtures run concurrently (latency then not comparable)")
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
        cfg = model_config(args)
        from app.llm import ollama
        if cfg.backend == "ollama" and ollama.is_remote(cfg.host) and not ollama.remote_allowed():
            raise SystemExit(f"{cfg.host} 是網際網路上的主機；遠端推論會把教材送到該主機，確認後設定 MENTOR_ALLOW_REMOTE_MODEL=1")
        prov = provenance(args, cfg, build_backend(cfg))
        if prov["remote"]:
            verdict = ("digest 與本機相同，品質結果可驗收；耗時不能驗收" if prov["digest_match"] else
                       f"digest 無法和本機比對（本機 {prov['local_model_digest']}），結果只供開發參考")
            print(f"[{prov['run_class']}] 推論送往 {cfg.host}（digest {prov['model_digest']}）：{verdict}", flush=True)
        asyncio.run(run(args, prov))


if __name__ == "__main__":
    main()
