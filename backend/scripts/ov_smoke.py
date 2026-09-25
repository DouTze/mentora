"""Smoke test for the OpenVINO backend, using the same prompts and parsers as the app.

    .venv/bin/python scripts/ov_smoke.py --model-path models/qwen3-8b-int4-ov --device GPU

Checks: device + model dir, model load, question generation from a text range (JSON valid?), and
answer judging for a few typical answers (JSON valid? verdict sensible?). Exit code 0 = all passed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.llm.openvino import OpenVINOBackend  # noqa: E402
from app.models import ModelJudgement  # noqa: E402
from app.tutor import generate, library  # noqa: E402
from app.tutor.session import GEN_CONTEXT_TOKENS, GEN_MAX_TOKENS, InvalidModelOutput, parse_judgement  # noqa: E402

SAMPLE = """# 相關不等於因果

夏天冰淇淋銷量上升時，溺水人數也會上升。兩者確實一起變動（相關），但不是吃冰淇淋造成溺水。
真正的原因是氣溫：天氣熱，買冰的人變多，去玩水的人也變多。這種同時影響兩件事的因素稱為「共同原因」或「干擾變數」。

# 反向因果

研究發現常運動的人比較快樂。但也可能是快樂的人比較有動力去運動，因果方向可能相反。
要確認因果，最可靠的方法是隨機對照實驗：把人隨機分成兩組，只改變一個因素，再比較結果。
"""

# (answer, verdicts we accept)
ANSWERS = [
    ("不同意。冰淇淋和溺水都是因為夏天氣溫高才一起增加，氣溫是共同原因，不是冰淇淋害人溺水。", {"correct"}),
    ("同意，數據都顯示一起上升了，所以應該禁止賣冰淇淋。", {"incorrect", "partial"}),  # both lead to a hint
    ("我不知道", {"incorrect", "unclear"}),
]


def show(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def perf(r) -> str:
    u = r.usage
    tps = u.get("tokens_per_sec")
    return (f"{r.latency_ms / 1000:.1f}s, in {u.get('prompt_tokens')} / out {u.get('completion_tokens')} tok"
            + (f", {tps:.1f} tok/s, ttft {u.get('ttft_ms', 0):.0f}ms" if tps else ""))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-path", default=config.OPENVINO_MODEL_PATH)
    ap.add_argument("--device", default=config.OPENVINO_DEVICE)
    ap.add_argument("--rounds", type=int, default=1, help="repeat generation N times to measure JSON stability")
    args = ap.parse_args()

    backend = OpenVINOBackend(args.model_path, args.device)
    health = await backend.health()
    print("devices:", health.get("devices"))
    if not show("health", health["ok"], health["detail"]):
        return 1

    t0 = time.perf_counter()
    await backend._pipeline()
    show("load", True, f"{time.perf_counter() - t0:.1f}s on {args.device}")

    ok = True
    material = {"id": "smoke", "title": "統計思考", "unit": "line"}
    messages = generate.build_messages(material, 1, 9, SAMPLE)
    content = None
    for i in range(args.rounds):
        r = await backend.generate(messages, temperature=0.2, max_tokens=GEN_MAX_TOKENS, context_tokens=GEN_CONTEXT_TOKENS,
                                   meta={"task": "generate", "json_schema": generate.Generated.model_json_schema()})
        try:
            gen = generate.parse(r.text)
            content = generate.to_content(gen, material, 1, 9, SAMPLE)
            show(f"generate #{i + 1}", True, f"{len(gen.concepts)} concepts: {[c.title for c in gen.concepts]} ({perf(r)})")
        except generate.InvalidGeneration as e:
            ok = show(f"generate #{i + 1}", False, f"{e} ({perf(r)})\n--- raw ---\n{r.text[:1500]}")

    content = content or library.load_content("correlation-causation")
    concept = content["concepts"][0]
    q = next(x for x in content["questions"] if x["concept_id"] == concept["id"])
    print(f"\n題目：{q['prompt']}\n")
    for answer, accepted in ANSWERS:
        prompt = library.render_prompt(library.load_prompt("assess"), {
            "content_title": content["title"], "concept_title": concept["title"], "concept_explanation": concept["explanation"],
            "question_type": "診斷題", "question_prompt": q["prompt"], "rubric": q["rubric"], "recent_turns": "（無）",
            "student_answer": answer, "allowed_actions": "給回饋、提出一個提示、追問理由",
            "concept_ids": ", ".join(c["id"] for c in content["concepts"]),
        })
        r = await backend.generate([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=512, context_tokens=8192,
                                   meta={"json_schema": ModelJudgement.model_json_schema()})
        try:
            j, notes = parse_judgement(r.text, {c["id"] for c in content["concepts"]})
            good = j.assessment in accepted
            ok = show(f"judge「{answer[:12]}…」", good, f"{j.assessment}/reasoning_ok={j.reasoning_ok} {notes or ''} ({perf(r)})\n       feedback: {j.feedback}\n       hint: {j.hint} probe: {j.probe}") and ok
        except InvalidModelOutput as e:
            ok = show(f"judge「{answer[:12]}…」", False, f"{e}\n--- raw ---\n{r.text[:800]}")

    print("\n結果：", "全部通過" if ok else "有失敗項目")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
