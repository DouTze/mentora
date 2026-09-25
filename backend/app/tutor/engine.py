"""Teaching rules. Pure functions: (state, judgement, strategy, content) -> decision + new state.

The model only supplies a ModelJudgement; everything about *what happens next* lives here,
parameterised by the strategy file so the flow can change without touching the API."""
from __future__ import annotations

import copy
from typing import Any

from ..models import ModelJudgement, TutorMessage

PHASES = ("diagnostic", "transfer")


def initial_state(content: dict) -> dict[str, Any]:
    order = [c["id"] for c in content["concepts"]]
    first = _question_for(content, order[0], "diagnostic")
    return {
        "concept_order": order,
        "concept_index": 0,
        "phase": "diagnostic",
        "question_id": first["id"] if first else None,
        "hint_count": 0,
        "probe_count": 0,
        "concepts": {cid: {"status": "not_started", "diagnostic": None, "transfer": None} for cid in order},
        "stuck_at": [],   # question ids where explanation had to be shown
    }


def _question_for(content: dict, concept_id: str, qtype: str) -> dict | None:
    for q in content["questions"]:
        if q["concept_id"] == concept_id and q["type"] == qtype:
            return q
    return None


def get_question(content: dict, qid: str | None) -> dict | None:
    return next((q for q in content["questions"] if q["id"] == qid), None) if qid else None


def get_concept(content: dict, cid: str) -> dict:
    return next(c for c in content["concepts"] if c["id"] == cid)


def current_concept_id(state: dict) -> str:
    return state["concept_order"][state["concept_index"]]


def decide(state: dict, judgement: ModelJudgement, strategy: dict, content: dict) -> tuple[dict, TutorMessage, dict]:
    """Return (new_state, message_for_student, decision_record)."""
    s = copy.deepcopy(state)
    q = get_question(content, s["question_id"])
    assert q is not None, "no active question"
    concept = get_concept(content, q["concept_id"])
    max_hints = int(strategy.get("max_hints_before_explain", 2))
    max_probes = int(strategy.get("max_probes_before_hint", 1))
    a = judgement.assessment
    rule = ""

    if a == "correct" and judgement.reasoning_ok:
        rule = "correct + reasoning_ok"
        action = "pass" if s["phase"] == "transfer" else "advance"
    elif a == "correct" and not judgement.reasoning_ok:
        rule = "correct + reasoning flawed"
        action = "probe"
    elif a == "unclear":
        rule = "unclear"
        action = "probe"
    else:  # partial / incorrect
        rule = "partial | incorrect"
        action = "hint"

    # Probing too many times counts as being stuck → fall through to a hint.
    if action == "probe":
        s["probe_count"] += 1
        if s["probe_count"] > max_probes:
            rule += " → probes exhausted"
            action = "hint"

    if action == "hint":
        s["hint_count"] += 1
        if s["hint_count"] > max_hints:
            rule += " → hints exhausted"
            action = "explain"

    msg = TutorMessage(action=action, feedback=judgement.feedback, source_ids=judgement.source_ids or [concept["id"]])

    if action == "probe":
        msg.probe = judgement.probe or "可以再說明一下你的理由嗎？"
    elif action == "hint":
        idx = min(s["hint_count"] - 1, len(q.get("hints", [])) - 1)
        msg.hint = judgement.hint or (q["hints"][idx] if idx >= 0 else "再想想有沒有其他可能的原因。")
    elif action == "explain":
        msg.explanation = concept["explanation"]
        msg.examples = list(concept.get("examples", []))
        s["stuck_at"].append(q["id"])
        _record_outcome(s, q, "needs_review")
        _move_on(s, content)
    elif action == "advance":
        _record_outcome(s, q, "passed" if s["hint_count"] == 0 else "passed_with_help")
        _move_on(s, content)
    elif action == "pass":
        _record_outcome(s, q, "passed" if s["hint_count"] == 0 else "passed_with_help")
        _move_on(s, content)

    decision = {
        "rule": rule,
        "action": action,
        "assessment": a,
        "reasoning_ok": judgement.reasoning_ok,
        "hint_count": s["hint_count"],
        "probe_count": s["probe_count"],
        "strategy_id": strategy["id"],
    }
    return s, msg, decision


def _record_outcome(s: dict, q: dict, outcome: str) -> None:
    c = s["concepts"][q["concept_id"]]
    c[q["type"]] = {"outcome": outcome, "hints": s["hint_count"], "probes": s["probe_count"]}
    if q["type"] == "transfer" or outcome == "needs_review":
        # Concept status: transfer result wins; any needs_review sticks.
        if outcome == "needs_review" or c.get("status") == "needs_review":
            c["status"] = "needs_review"
        else:
            c["status"] = outcome
    else:
        c["status"] = "in_progress"


def _move_on(s: dict, content: dict) -> None:
    s["hint_count"] = 0
    s["probe_count"] = 0
    cid = current_concept_id(s)
    if s["phase"] == "diagnostic":
        nxt = _question_for(content, cid, "transfer")
        if nxt:
            s["phase"] = "transfer"
            s["question_id"] = nxt["id"]
            return
    # finished this concept
    s["concept_index"] += 1
    if s["concept_index"] >= len(s["concept_order"]):
        s["phase"] = "completed"
        s["question_id"] = None
        return
    s["phase"] = "diagnostic"
    s["question_id"] = _question_for(content, current_concept_id(s), "diagnostic")["id"]  # type: ignore[index]


def build_summary(state: dict, content: dict, turns: list[dict]) -> dict:
    """Summary is assembled from records, not generated by the model."""
    practiced, stuck, review = [], [], []
    for cid in state["concept_order"]:
        c = get_concept(content, cid)
        st = state["concepts"][cid]
        practiced.append({"concept_id": cid, "title": c["title"], "status": st["status"],
                          "diagnostic": st["diagnostic"], "transfer": st["transfer"]})
        if st["status"] == "needs_review":
            review.append({"concept_id": cid, "title": c["title"], "explanation": c["explanation"]})
    for qid in state.get("stuck_at", []):
        q = get_question(content, qid)
        if q:
            stuck.append({"question_id": qid, "concept_id": q["concept_id"], "prompt": q["prompt"]})
    errors = [t for t in turns if t.get("error")]
    return {
        "practiced": practiced,
        "stuck_at": stuck,
        "review_next": review,
        "turn_count": len(turns),
        "error_count": len(errors),
        "verdict": "passed" if not review else "needs_review",
    }
