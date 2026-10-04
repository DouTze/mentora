"""Opt-in R8 measurement: what the reader waits for, with prefetch on and off (same model, material, reading time).

    cd backend
    .venv/bin/python scripts/prefetch_probe.py --model qwen3-vl:4b --case cross-window-argument --read-seconds 20

Runs the real app (LangGraph workflow, isolated temporary data dir, local Ollama) through one session per mode:
start → first guide, then for every unit "read" for --read-seconds and press next. Reports the first-guide wait,
the wait after each next (p50 / p95), all model calls, and the background calls spent on prefetch (adopted or
wasted). The reading time is an assumption of the measurement, stated in the output. Nothing leaves the machine."""
from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def run_mode(prefetch: bool, args, text: str, name: str) -> dict:
    from fastapi.testclient import TestClient

    from app import config
    from app.db import Database
    from app.llm.registry import Registry
    from app.main import create_app
    from app.models import ModelConfig

    config.WORKFLOW = "langgraph"
    config.READING_PREFETCH = prefetch
    with tempfile.TemporaryDirectory() as td:
        config.READING_DIR = Path(td) / "reading"
        cfg = ModelConfig(backend="ollama", model=args.model, host=args.host, temperature=0.2, max_tokens=512, context_tokens=8192)
        app = create_app(Database(Path(td) / "db.sqlite3"), Registry(cfg))
        with TestClient(app) as c:
            def idle(sid, timeout=1800):
                end = time.time() + timeout
                while time.time() < end:
                    s = c.get(f"/api/sessions/{sid}").json()
                    if not s["workflow"]["active_command"]:
                        return s
                    time.sleep(0.2)
                raise SystemExit("timeout")
            m = c.post("/api/materials", files={"file": (name, io.BytesIO(text.encode()))}).json()
            t0 = time.perf_counter()
            s = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": len(text.splitlines())}).json()
            s = idle(s["id"])
            first = time.perf_counter() - t0
            sid, waits, k = s["id"], [], 0
            while s["workflow"]["waiting"] == "student" and k < 20:
                time.sleep(args.read_seconds)               # the reader reads the guide (prefetch may run meanwhile)
                t1 = time.perf_counter()
                c.post(f"/api/sessions/{sid}/events", json={"command_id": f"n{k}", "type": "next"})
                s = idle(sid)
                waits.append(time.perf_counter() - t1)
                k += 1
                if s["workflow"]["waiting"] != "student":
                    break
            runner = app.state.runner
            calls = app.state.repo.list_llm_calls(sid)
            prefetch_calls = [x for x in calls if (x["operation_key"] or "").find(":prefetch:") >= 0]
            adopted = sum(1 for g in (app.state.db.get_session(sid)["state"].get("reading") or {}).get("guides", {}).values()
                          if g.get("prefetched"))
            return {"prefetch": prefetch, "first_guide_s": round(first, 1), "next_waits_s": [round(w, 1) for w in waits],
                    "next_wait_p50_s": round(pct(waits, 50), 1) if waits else None,
                    "next_wait_p95_s": round(pct(waits, 95), 1) if waits else None,
                    "model_calls": len(calls), "prefetch_calls": len(prefetch_calls), "prefetched_guides_adopted": adopted,
                    "prefetch_log": [x[1] for x in runner.prefetch_log],
                    "input_tokens": sum((x["usage"] or {}).get("prompt_tokens") or 0 for x in calls),
                    "output_tokens": sum((x["usage"] or {}).get("completion_tokens") or 0 for x in calls),
                    "final_waiting": s["workflow"]["waiting"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-vl:4b")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--case", default="cross-window-argument")
    ap.add_argument("--read-seconds", type=float, default=20.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    from evals import reading_eval as ev
    case = ev.select_cases(ev.load_manifest(), [args.case])[0]
    path = ev.EVAL_DIR / case["source"]
    text = path.read_text(encoding="utf-8")
    report = {"model": args.model, "case": args.case, "read_seconds_assumed": args.read_seconds,
              "runs": [run_mode(False, args, text, path.name), run_mode(True, args, text, path.name)]}
    out = json.dumps(report, ensure_ascii=False, indent=1)
    print(out)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
