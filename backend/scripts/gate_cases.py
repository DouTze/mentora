"""Gate replay: real guide runs, replayed against the current gate rules without a model.

    cd backend
    .venv/Scripts/python scripts/gate_cases.py capture            # data → data/evals/gate-cases/cases/*.json
    .venv/Scripts/python scripts/gate_cases.py review             # every draft of every case → review.md, to label
    .venv/Scripts/python scripts/gate_cases.py replay             # run the current graph on the recorded outputs

capture: every guided-reading run becomes a case — its initial state (the guide_input artifact the graph writes when
it starts; for older runs, the LangGraph checkpoints if they still hold it), the session's pinned prompts / model
snapshot / quotes, every model call it made (llm_calls under its operation key, in order) and every draft it wrote
(its cached step updates). Source-stage calls the run took from the verified-source cache are taken from the latest
run that understood the same unit before it.

replay: the current reading graph runs each case with a cassette backend: a node's k-th model call gets the k-th
recorded output of that node (a recorded transport failure is raised again). The gate rules are today's; the model
outputs are the ones the run really got. A call whose prompt differs from the recorded one is marked "diverged"
(the recorded output answers another question; the verdict is then a hint only); a call the run never made is
"unrecorded" and stops the case — it needs a real model.

labels (data/evals/gate-cases/labels.json): a human verdict per draft (or per source understanding, when the run
stopped at Gate 1), keyed by its hash as printed in review.md: {"<hash>": {"ok": true|false, "note": "…", "by": "…"}}.
"ok" = fit to show a student as is. The replay report counts, per case, a correct pass / correct hold / wrong hold
(the gate held an acceptable draft) / wrong pass (it published a bad one), and per finding code how often it held an
acceptable draft.

The cases hold the source text of the user's materials, so they stay under data/ (git-ignored)."""
from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.llm.base import Capabilities, DegenerateOutput, LLMBackend, LLMResult  # noqa: E402
from app.llm.runtime import BackendPool, ListSink, ModelRuntime, ModelSnapshot  # noqa: E402
from app.reading import gates  # noqa: E402
from app.reading.tools import ReadingScope  # noqa: E402
from app.workflows import invoke  # noqa: E402
from app.workflows.reading_graph import ReadingDeps, build_reading_graph  # noqa: E402

OUT = config.DATA_DIR / "evals" / "gate-cases"
SOURCE_NODES = ("symbols", "understand", "source_audit", "revise_source", "context")


def digest(x) -> str:
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def prompt_text(messages: list) -> str:
    """The text of a request's messages (images left out: they are rendered anew and compared by their region)."""
    out = []
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(c, list):
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        out.append(str(c))
    # a rendered region is named by its PNG's hash; replay does not render the PDF again
    return re.sub(r"（截圖 [0-9a-f]+）", "（截圖）", "\n".join(out))


# -- capture ---------------------------------------------------------------------------------------

def checkpoint_inputs(ckpt_path: Path) -> list[dict]:
    """Initial states of the guide runs still in the LangGraph checkpoints (runs from before guide_input existed)."""
    if not ckpt_path.exists():
        return []
    from langgraph.checkpoint.sqlite import SqliteSaver
    conn = sqlite3.connect(ckpt_path, check_same_thread=False)
    saver = SqliteSaver(conn)
    out = []
    for tid, ns in conn.execute("SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints WHERE checkpoint_ns LIKE 'guide:%'"):
        for c in list(saver.list({"configurable": {"thread_id": tid, "checkpoint_ns": ns}}))[::-1]:
            if c.checkpoint["channel_values"].get("task"):
                out.append({"state0": c.checkpoint["channel_values"], "start": c.checkpoint["ts"]})
                break
    return out


def run_inputs(db: sqlite3.Connection) -> list[dict]:
    """Every run's initial state: the guide_input artifacts, plus older runs only the checkpoints still hold."""
    runs = {}
    for payload, created in db.execute("SELECT payload, created_at FROM reading_artifacts WHERE kind = 'guide_input' "
                                       "ORDER BY rowid"):
        s0 = json.loads(payload)
        runs[s0["op"]] = {"state0": s0, "start": created}
    for r in checkpoint_inputs(Path(config.CHECKPOINT_PATH or config.DB_PATH.parent / "checkpoints.sqlite3")):
        runs.setdefault(r["state0"]["op"], r)
    return list(runs.values())


CALL_COLS = ("node", "purpose", "operation_key", "request", "raw", "error", "error_kind", "created_at")


def _calls(db: sqlite3.Connection, where: str, args: tuple) -> list[dict]:
    q = f"SELECT {', '.join(CALL_COLS)} FROM llm_calls WHERE {where} ORDER BY rowid"
    return [dict(zip(CALL_COLS, r)) for r in db.execute(q, args)]


def calls_for(db: sqlite3.Connection, op: str, unit: str, start: str) -> list[dict]:
    own = _calls(db, "operation_key LIKE ?", (op + ":%",))
    calls = own
    if not any(r["node"] == "understand" for r in own):
        # the verified source came from the cache: the calls of the latest run (of any session — a verified source
        # is shared between sessions reading the same unit) that understood this unit before this run started
        earlier = [r for r in _calls(db, "operation_key LIKE ? AND node = 'understand' AND created_at <= ?",
                                     (f"%:{unit}:%", start[:19] + "~"))
                   if not r["operation_key"].startswith(op + ":")]
        if earlier:
            src = earlier[-1]["operation_key"].rsplit(":", 2)[0]
            calls = [r for r in _calls(db, "operation_key LIKE ?", (src + ":%",)) if r["node"] in SOURCE_NODES] + own
    out = []
    for r in calls:
        req = json.loads(r["request"] or "{}")
        out.append({"node": r["node"], "purpose": r["purpose"], "raw": r["raw"], "error": r["error"],
                    "error_kind": r["error_kind"], "prompt": prompt_text(req.get("messages")),
                    "context_limit": (req.get("context_budget") or {}).get("limit")})
    return out


def recorded_outcome(db: sqlite3.Connection, s0: dict, session_state: dict) -> dict:
    """What the run ended with, for the report: a held run left a rejected artifact; otherwise it was published
    (prefetched, or in the session's guides) or only verified its source (warm)."""
    op, unit = s0["op"], s0["task"]["unit_id"]
    row = db.execute("SELECT payload FROM reading_artifacts WHERE operation_key = ?", (op + ":rejected",)).fetchone()
    if row:
        return {"status": "failed", "error": json.loads(row[0]).get("error")}
    if s0.get("source_only"):
        return {"status": "source_only", "error": None}
    pre = op[:-len(":run")] if op.endswith(":run") else None
    guides = ((session_state or {}).get("reading") or {}).get("guides") or {}
    if (pre and db.execute("SELECT 1 FROM reading_artifacts WHERE operation_key = ?", (pre,)).fetchone()) or \
            any(g.get("unit_id") == unit for g in guides.values()):
        return {"status": "accepted", "error": None}
    return {"status": "unknown", "error": None}


def capture(args) -> None:
    db = sqlite3.connect(config.DB_PATH)
    sessions = {}
    # cases are kept, never cleared: deleting a learning record deletes its calls and checkpoints, so a case file
    # can be the only copy left of a run; a run captured again overwrites its own file (same id)
    (OUT / "cases").mkdir(parents=True, exist_ok=True)
    n = 0
    for run in run_inputs(db):
        s0 = run["state0"]
        t = s0["task"]
        sid, op = t["session_id"], s0["op"]
        if sid not in sessions:
            row = db.execute("SELECT workflow_meta, content_snapshot, state FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                continue
            sessions[sid] = tuple(json.loads(x) for x in row)
        meta, content, st = sessions[sid]
        calls = calls_for(db, op, t["unit_id"], run["start"])
        if not calls:
            continue
        # every draft and understanding the run produced: its model steps' cached updates
        drafts, understandings = {}, {}
        for (payload,) in db.execute("SELECT payload FROM reading_artifacts WHERE kind = 'step' AND operation_key LIKE ? "
                                     "ORDER BY rowid", (op + ":%",)):
            u = json.loads(payload)
            if (u.get("draft") or {}).get("guide"):
                drafts.setdefault(digest(u["draft"]["guide"]), u["draft"]["guide"])
            if u.get("understanding"):
                understandings.setdefault(digest(u["understanding"]), u["understanding"])
        kind = "warm" if s0.get("source_only") else "prefetch" if op.endswith(":run") or ":prefetch:" in op else "guide"
        case_id = f"{sid}-{t['unit_id']}-{kind}-a{t['attempt']}-{digest(op)[:6]}"
        case = {"id": case_id, "session_id": sid, "unit_id": t["unit_id"], "label": t["label"], "kind": kind,
                "start": run["start"], "state0": s0, "prompts": meta["prompt_bundle"]["prompts"],
                "model_snapshot": meta["model_snapshot"], "quotes": content["quotes"], "calls": calls,
                "recorded": recorded_outcome(db, s0, st), "drafts": drafts, "understandings": understandings}
        (OUT / "cases" / f"{case_id}.json").write_text(json.dumps(case, ensure_ascii=False, indent=1, default=str),
                                                       encoding="utf-8")
        n += 1
    print(f"{n} 個案例 → {OUT / 'cases'}")


# -- review ----------------------------------------------------------------------------------------

def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((OUT / "cases").glob("*.json"))]


def load_labels() -> dict:
    p = OUT / "labels.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def review(args) -> None:
    labels = load_labels()
    lines = ["# 待標記的草稿", "", "每份草稿標記「能不能直接給學生看」，寫進 labels.json（以雜湊為鍵）。", ""]
    for c in load_cases():
        lines += [f"## {c['id']}（{c['label']}）", "", f"線上結果：{c['recorded']['status']}"
                  + (f" — {c['recorded']['error']['detail'][:300]}" if c["recorded"].get("error") else ""), "",
                  "### 原文", "", c["state0"]["task"]["text"], ""]
        for h, g in c["drafts"].items():
            lab = labels.get(h)
            lines += [f"### 草稿 `{h}`" + (f"（已標記：{'可' if lab['ok'] else '不可'}）" if lab else ""), "", g, ""]
        if not c["drafts"]:
            for h, u in c["understandings"].items():
                lines += [f"### 來源理解 `{h}`", "", "\n".join(f"- {t['id']} {t['zh']}" for t in u.get("translations") or []), ""]
    (OUT / "review.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"→ {OUT / 'review.md'}")


# -- replay ----------------------------------------------------------------------------------------

class Unrecorded(Exception):
    pass


class Cassette(LLMBackend):
    name = "replay"
    model = "cassette"

    def __init__(self, calls: list[dict]):
        self.queues: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        for c in calls:
            self.queues[c["node"]].append(c)
        limit = next((c["context_limit"] for c in calls if c.get("context_limit")), None)
        self.caps = Capabilities(context_limit=limit, vision=True, source="replay")
        self.diverged: list[str] = []
        self.played: list[str] = []

    async def capabilities(self) -> Capabilities:
        return self.caps

    async def health(self) -> dict:
        return {"ok": True}

    async def generate(self, messages, *, temperature, max_tokens, context_tokens, meta=None) -> LLMResult:
        node = (meta or {}).get("step") or "context"
        if not self.queues[node]:
            raise Unrecorded(node)
        rec = self.queues[node].popleft()
        self.played.append(node)
        if prompt_text(messages) != rec["prompt"]:
            self.diverged.append(node)
        if rec["raw"] is None:
            raise DegenerateOutput(rec["error"] or "recorded failure")
        return LLMResult(text=rec["raw"], latency_ms=0)


async def replay_case(c: dict) -> dict:
    tape = Cassette(c["calls"])
    snap = ModelSnapshot.from_config(c["model_snapshot"])
    pool = BackendPool(build=lambda _: tape)
    deps = ReadingDeps(runtime_for=lambda s: ModelRuntime(snap, pool), prompts_for=lambda s: c["prompts"],
                       scope_for=lambda s, i: ReadingScope(quotes=c["quotes"], current_index=i),
                       sink=ListSink(), vsrc_cache={}, render_region=lambda *a, **k: b"")
    state = {**c["state0"], "gate_rules": gates.GATE_RULES_VERSION}
    try:
        out = await invoke(build_reading_graph(deps), state, {"configurable": {"thread_id": c["id"]}})
    except Unrecorded as e:
        return {"status": "unrecorded", "node": str(e), "diverged": tape.diverged}
    held = [f for f in out.get("findings") or [] if f["severity"] == "major"]
    g1 = [ch for ch in (out.get("gate1") or {}).get("checks") or [] if ch["status"] in ("fail", "unverified")]
    draft = (out.get("draft") or {}).get("guide")
    # every draft the gates judged: those sent back for revision (history) and the last one (published or held)
    judged = [(digest(h["draft"]["guide"]), sorted({f["code"] for f in h["findings"] if f["severity"] == "major"}))
              for h in out.get("history") or [] if h.get("stage") == "body" and (h.get("draft") or {}).get("guide")]
    if draft:
        judged.append((digest(draft), [] if out["status"] == "accepted" else sorted({f["code"] for f in held})))
    return {"status": out["status"], "error": out.get("error"), "diverged": tape.diverged,
            "draft": digest(draft) if draft else None,
            "understanding": digest(out["understanding"]) if out.get("understanding") else None,
            "codes": sorted({f["code"] for f in held}) if out.get("verified") else sorted({ch["kind"] for ch in g1}),
            "judged": judged, "calls": (out.get("budget") or {}).get("calls")}


def verdict(r: dict, labels: dict) -> str:
    if r["status"] == "unrecorded":
        return "需要真實模型"
    lab = labels.get(r["draft"] or "") or labels.get(r["understanding"] or "")
    if lab is None:
        return "未標記"
    published = r["status"] == "accepted"
    return {(True, True): "正確放行", (False, False): "正確擋下", (False, True): "誤擋", (True, False): "漏放"}[(published, lab["ok"])]


def replay(args) -> None:
    labels = load_labels()
    rows, wrong_codes = [], collections.Counter()
    for c in load_cases():
        if args.case and args.case not in c["id"]:
            continue
        r = asyncio.run(replay_case(c))
        v = verdict(r, labels)
        if v == "誤擋":
            wrong_codes.update(r["codes"])
        rows.append((c, r, v))
    print(f"規則版本 {gates.GATE_RULES_VERSION}\n")
    print("| 案例 | 線上 | 重播 | 判定 | 擋下原因 | 備註 |\n|---|---|---|---|---|---|")
    for c, r, v in rows:
        note = (f"提示詞不同：{','.join(dict.fromkeys(r['diverged']))}" if r.get("diverged") else "") + \
               (f" 缺錄音：{r['node']}" if r["status"] == "unrecorded" else "")
        print(f"| {c['id']} | {c['recorded']['status']} | {r['status']} | {v} | {', '.join(r.get('codes') or [])} | {note} |")
    tally = collections.Counter(v for _, _, v in rows)
    print("\n最後結果：" + "、".join(f"{k} {n}" for k, n in tally.most_common()))
    # per judged draft: a good draft sent to revision is a false flag too — revisions can break it
    per, flag_codes = collections.Counter(), collections.Counter()
    for _, r, _ in rows:
        for h, codes in r.get("judged") or []:
            lab = labels.get(h)
            if lab is None:
                per["未標記"] += 1
                continue
            k = {(True, True): "好稿放行", (True, False): "好稿被判有問題", (False, False): "壞稿被抓到",
                 (False, True): "壞稿放行"}[(lab["ok"], not codes)]
            per[k] += 1
            if k == "好稿被判有問題":
                flag_codes.update(codes)
    print("每份草稿：" + "、".join(f"{k} {n}" for k, n in per.most_common()))
    if flag_codes:
        print("好稿被判有問題時的代碼：" + "、".join(f"{k} {n}" for k, n in flag_codes.most_common()))
    if wrong_codes:
        print("誤擋時出現的問題代碼：" + "、".join(f"{k} {n}" for k, n in wrong_codes.most_common()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capture")
    sub.add_parser("review")
    p = sub.add_parser("replay")
    p.add_argument("--case", help="只重播 id 含這段文字的案例")
    args = ap.parse_args()
    {"capture": capture, "review": review, "replay": replay}[args.cmd](args)


if __name__ == "__main__":
    main()
