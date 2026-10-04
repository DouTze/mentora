"""Opt-in R6 measurement: read a whole fixture through the real app and record the verified notes and the quiz.

    cd backend
    .venv/bin/python scripts/session_probe.py --model qwen3-vl:4b --cases headings,early-quiz --out data/evals/r9-sessions

For each case a LangGraph session (isolated temporary data dir; Ollama at --host, local by default) reads every unit ("next"); a held guide
is retried once, and if it is still held the reader finishes early so the quiz is built from what was read. Writes
<out>/results.json (guide statuses, notes with their verification, quiz plan, concepts / questions with their basis,
dropped questions, model calls per node, where the model ran) and <out>/spotcheck.md: every note and question next to the verified text
it cites, for the human checks of RUBRIC.md (note lost a condition / fabricated; question unanswerable / wrong
condition / outside the read range). Nothing is downloaded; an internet --host needs MENTOR_ALLOW_REMOTE_MODEL=1."""
from __future__ import annotations

import argparse
import collections
import io
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def probe_case(case: dict, args) -> dict:
    from fastapi.testclient import TestClient

    from app import config
    from app.db import Database
    from app.llm.registry import Registry
    from app.main import create_app
    from app.models import ModelConfig
    from evals import reading_eval as ev

    path = ev.EVAL_DIR / case["source"]
    config.WORKFLOW = "langgraph"
    config.READING_PREFETCH = False
    with tempfile.TemporaryDirectory() as td:
        config.READING_DIR = Path(td) / "reading"
        cfg = ModelConfig(backend=args.backend, model=args.model, host=args.host, temperature=0.2, max_tokens=512, context_tokens=8192)
        app = create_app(Database(Path(td) / "db.sqlite3"), Registry(cfg))
        with TestClient(app) as c:
            def idle(sid, timeout=3600):
                end = time.time() + timeout
                while time.time() < end:
                    s = c.get(f"/api/sessions/{sid}").json()
                    if not s["workflow"]["active_command"]:
                        return s
                    time.sleep(0.3)
                raise SystemExit("timeout")

            def send(sid, type_, cid, **kw):
                c.post(f"/api/sessions/{sid}/events", json={"command_id": cid, "type": type_, **kw})
                return idle(sid)

            data = path.read_bytes()
            m = c.post("/api/materials", files={"file": (path.name, io.BytesIO(data))}).json()
            n = m["unit_count"]
            t0 = time.perf_counter()
            s = idle(c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": n}).json()["id"])
            sid, k, events = s["id"], 0, []
            while k < 40:
                wf = s["workflow"]
                if wf["waiting"] == "retry":
                    events.append({"held": wf.get("failed_node"), "error": (wf.get("error") or {}).get("code")})
                    if len([e for e in events if "held" in e]) <= 1 and (wf.get("error") or {}).get("retryable", True):
                        s = send(sid, "retry", f"r{k}", payload={"target_command_id": wf["failed_command_id"]})
                    elif wf.get("failed_node") == "guide":
                        s = send(sid, "finish", f"f{k}")             # quiz from what was read
                    else:
                        break
                elif wf["waiting"] == "student":
                    s = send(sid, "next", f"n{k}")
                else:
                    break
                k += 1
            total_s = time.perf_counter() - t0
            row = app.state.db.get_session(sid)
            rd, content = row["state"].get("reading") or {}, row["content_snapshot"]
            calls = app.state.repo.list_llm_calls(sid)
            per_node = collections.Counter(x["node"].split(".")[0] for x in calls)
            claims = {f"Q{int(i) + 1}.{cl['id']}": cl["statement"] for i, g in rd.get("guides", {}).items()
                      for cl in g.get("source_claims") or []}
            notes = {f"P{int(pi) + 1}.{n['id']}": n for pi, cp in (rd.get("compacts") or {}).items() for n in cp.get("notes") or []}
            return {"id": case["id"], "units": len(content["quotes"]), "guides": sorted(rd.get("guides", {}), key=int),
                    "events": events, "final_waiting": s["workflow"]["waiting"], "status": row["status"],
                    "compacts": {pi: {k2: cp.get(k2) for k2 in ("status", "reason", "gaps", "revised")} | {
                        "notes": [{x: n[x] for x in ("id", "kind", "text", "claim_refs", "status", "reasons")} for n in cp.get("notes") or []]}
                        for pi, cp in (rd.get("compacts") or {}).items()},
                    "quiz_plan": rd.get("quiz_plan"), "quiz_scope": rd.get("quiz_scope"),
                    "concepts": [{x: cc.get(x) for x in ("id", "title", "explanation", "basis", "source_refs")} for cc in content.get("concepts") or []],
                    "questions": [{x: q.get(x) for x in ("id", "concept_id", "type", "prompt", "rubric", "basis")} for q in content.get("questions") or []],
                    "basis_text": {**claims, **{k2: n["text"] for k2, n in notes.items()}},
                    "calls": len(calls), "calls_by_node": dict(per_node), "total_s": round(total_s, 1),
                    "input_tokens": sum((x["usage"] or {}).get("prompt_tokens") or 0 for x in calls),
                    "output_tokens": sum((x["usage"] or {}).get("completion_tokens") or 0 for x in calls)}


def spotcheck(results: list[dict]) -> str:
    lines = ["# R6 抽查表（筆記與題目）", "", "依 evals/reading/RUBRIC.md「筆記與題目」：每條筆記對照它引用的命題，每道題目對照它的依據。", ""]
    for r in results:
        lines += [f"## {r['id']}", "", f"導讀 {len(r['guides'])}／{r['units']} 段；結束狀態 {r['final_waiting']}；事件 {r['events']}", ""]
        for pi, cp in sorted(r["compacts"].items(), key=lambda x: int(x[0])):
            lines += [f"### 部分 {int(pi) + 1} 的筆記（{cp['status']}；未被筆記涵蓋的命題 {cp['gaps']}）", ""]
            for n in cp["notes"]:
                lines += [f"- **{n['id']}**［{n['status']}］{n['text']}（依據 {'、'.join(n['claim_refs'])}）" + (f" — 未通過：{'；'.join(n['reasons'])}" if n["reasons"] else "")]
            lines.append("")
        lines += ["### 題目", ""]
        for q in r["questions"]:
            basis = "；".join(f"{b}：{r['basis_text'].get(b, '（見筆記）')}" for b in q.get("basis") or [])
            lines += [f"- **{q['id']}**（{q['type']}）{q['prompt']}", f"  - 評分：{q['rubric']}", f"  - 依據：{basis}"]
        dropped = (r.get("quiz_plan") or {}).get("dropped")
        if dropped:
            lines += ["", f"被移除的概念：{dropped}"]
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["ollama", "mock"], default="ollama")
    ap.add_argument("--model", default="qwen3-vl:4b")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--cases", default="headings,early-quiz")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from evals import reading_eval as ev
    from app.llm import ollama
    remote = args.backend == "ollama" and ollama.is_remote(args.host)
    if remote and not ollama.remote_allowed():
        raise SystemExit(f"{args.host} 是網際網路上的主機；遠端推論會把教材送到該主機，確認後設定 MENTOR_ALLOW_REMOTE_MODEL=1")
    import asyncio
    digest = asyncio.run(ollama.OllamaBackend(args.host, args.model).model_digest()) if args.backend == "ollama" else None
    local = (asyncio.run(ollama.OllamaBackend("http://127.0.0.1:11434", args.model).model_digest())
             or ollama.manifest_digest(args.model)) if remote else digest
    prov = {"host": args.host if args.backend == "ollama" else None, "seed": ollama.sampling_seed(),
            "model_digest": digest, "local_model_digest": local,
            "run_class": "mock" if args.backend == "mock" else
            ("remote-quality" if digest and digest == local else "remote-unverified") if remote else "local-acceptance"}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for case in ev.select_cases(ev.load_manifest(), args.cases.split(","), include_broken=False):
        print(f"[session] {case['id']} …", flush=True)
        results.append(probe_case(case, args))
        (out / "results.json").write_text(json.dumps({"model": args.model, "provenance": prov, "results": results},
                                                     ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    (out / "spotcheck.md").write_text(spotcheck(results), encoding="utf-8")
    print(json.dumps([{k: r[k] for k in ("id", "units", "guides", "final_waiting", "calls", "total_s")} for r in results],
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
