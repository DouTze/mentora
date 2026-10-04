"""R8 comparison report from eval result directories (same model, parameters and fixtures).

    cd backend
    .venv/bin/python scripts/r8_report.py legacy=data/evals/r8-legacy prechange=data/evals/r0-graph-prechange \
        graph=data/evals/r8-graph --out data/evals/r8-report.md

Per run: units published / total and why the rest were held, per-unit latency p50 / p95, first-guide wait, model
calls and tokens (per node for graph runs), the token estimate against Ollama's actual prompt count, and the
automatic keep signals (guide + 中文課文, and the 主要正文 alone). Automatic signals are not quality scores."""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def load(path: str) -> dict:
    return json.loads((Path(path) / "results.json").read_text(encoding="utf-8"))


def summarize(name: str, run: dict) -> dict:
    res = [r for r in run["results"] if "expect_import_error" not in r]
    lat = [x for r in res for x in r.get("guide_latency_ms", [])]
    first = [r.get("first_guide_ms") for r in res if r.get("first_guide_ms")]
    held = collections.Counter()
    units = published = 0
    for r in res:
        if "guide_status" in r:
            for s in r["guide_status"]:
                units += 1
                if s["status"] == "accepted":
                    published += 1
                else:
                    held[(s.get("error") or {}).get("code") or "?"] += 1
        else:
            gs = r.get("guides") or []
            units += len(gs)
            published += sum(1 for g in gs if g)
            for e in r.get("guide_errors", []):
                held[e.split(": ")[1] if ": " in e else e[:40]] += 1
    calls = [c for r in res for c in r.get("calls", [])]
    per_node = collections.defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "ms": 0})
    ratio = []
    for r in res:
        for t in r.get("llm_trace", []):
            n = per_node[t["node"].split(".")[0]]
            u = t.get("usage") or {}
            n["calls"] += 1
            n["in"] += u.get("prompt_tokens") or 0
            n["out"] += u.get("completion_tokens") or 0
            n["ms"] += t.get("latency_ms") or 0
            if t.get("estimated_input") and u.get("prompt_tokens"):
                ratio.append(t["estimated_input"] / u["prompt_tokens"])
    s = run.get("summary", {})
    return {"name": name, "model": run["backend"].get("resolved_model"), "units": units, "published": published,
            "held": dict(held), "p50_s": round(pct(lat, 50) / 1000, 1) if lat else None,
            "p95_s": round(pct(lat, 95) / 1000, 1) if lat else None,
            "first_p50_s": round(pct(first, 50) / 1000, 1) if first else None, "total_guide_s": round(sum(lat) / 1000),
            "calls": len(calls), "in_tokens": sum((c.get("usage") or {}).get("prompt_tokens") or 0 for c in calls),
            "out_tokens": sum((c.get("usage") or {}).get("completion_tokens") or 0 for c in calls),
            "keep_ratio": s.get("keep_ratio"), "keep_ratio_guide_only": s.get("keep_ratio_guide_only"),
            "integrity_ok": s.get("integrity_ok"), "boundary_ok": s.get("boundary_ok"),
            "together_violations": s.get("together_violations"), "forbidden_hits": s.get("forbidden_hits"),
            "per_node": dict(per_node),
            "estimate_over_actual": (round(min(ratio), 2), round(sum(ratio) / len(ratio), 2), round(max(ratio), 2)) if ratio else None}


def markdown(rows: list[dict]) -> str:
    keys = [("模型", "model"), ("單元（導讀數）", "units"), ("公開", "published"), ("每段 p50 秒", "p50_s"), ("每段 p95 秒", "p95_s"),
            ("首段 p50 秒", "first_p50_s"), ("導讀總秒數", "total_guide_s"), ("模型呼叫", "calls"), ("輸入 tokens", "in_tokens"),
            ("輸出 tokens", "out_tokens"), ("來源完整性", "integrity_ok"), ("邊界符合 gold", "boundary_ok"),
            ("自動保留率（正文＋課文）", "keep_ratio"), ("自動保留率（只看正文）", "keep_ratio_guide_only"), ("forbidden", "forbidden_hits")]
    lines = ["| 指標 | " + " | ".join(r["name"] for r in rows) + " |", "| --- |" + " --- |" * len(rows)]
    for label, k in keys:
        lines.append(f"| {label} | " + " | ".join(str(r.get(k)) for r in rows) + " |")
    lines.append("| 未公開原因 | " + " | ".join("、".join(f"{a}×{b}" for a, b in r["held"].items()) or "—" for r in rows) + " |")
    lines.append("| 估計／實際輸入 tokens（min／avg／max） | " + " | ".join(str(r["estimate_over_actual"]) for r in rows) + " |")
    out = ["\n".join(lines), ""]
    for r in rows:
        if r["per_node"]:
            out += [f"### {r['name']}：各節點", "", "| 節點 | 呼叫 | 輸入 | 輸出 | 秒 |", "| --- | --- | --- | --- | --- |"]
            out += [f"| {n} | {v['calls']} | {v['in']} | {v['out']} | {round(v['ms'] / 1000)} |" for n, v in sorted(r["per_node"].items())]
            out.append("")
    return "\n".join(out)


def main() -> None:
    args = sys.argv[1:]
    out = None
    if "--out" in args:
        i = args.index("--out")
        out = args[i + 1]
        args = args[:i] + args[i + 2:]
    rows = []
    for a in args:
        name, path = a.split("=", 1) if "=" in a else (a, a)
        rows.append(summarize(name, load(path)))
    md = markdown(rows)
    print(md)
    if out:
        Path(out).write_text(md + "\n", encoding="utf-8")
        Path(out).with_suffix(".json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
