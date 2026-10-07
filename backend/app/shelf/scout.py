"""The bookshelf scout: a small LangGraph that guesses what a person wants to read next and finds it on their disk.

    profile (model)  reading tendencies from what they imported, read and typed: a label and search keywords each
    search  (code)   rank the file index with each tendency's keywords; PDF titles / pages for the top candidates
    pick    (model)  choose the shelf among those candidates, one short reason per book

The model never sees a path and never chooses a file it was not shown: `pick` answers with candidate numbers,
which code checks. Either model step may fail (no model, bad JSON, timeout); the graph then falls back to the keyword
ranking, so the shelf is never empty because of the model."""
from __future__ import annotations

import asyncio
import json
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from ..llm.runtime import ModelRuntime
from ..llm.tasks import parse_model_json
from .books import BookMeta, keywords_of, rank
from .index import ShelfIndex

SHELF_SIZE = 8
MIN_PICKS = 4
CANDIDATES = 20
CALL_TIMEOUT_S = 90


class Tendency(BaseModel):
    label: str = Field(max_length=16, description="2–8 個字的閱讀傾向名稱，例如「強化學習」「推理小說」")
    keywords: list[str] = Field(min_length=2, max_length=10, description="在檔名與資料夾名稱裡找書用的詞，中英文都列")


class Profile(BaseModel):
    tendencies: list[Tendency] = Field(min_length=1, max_length=4)


class Pick(BaseModel):
    n: int = Field(description="候選清單的編號")
    reason: str = Field(max_length=40, description="為什麼這本適合現在讀，20 字內")


class Picks(BaseModel):
    picks: list[Pick] = Field(max_length=SHELF_SIZE)


class ScoutState(TypedDict, total=False):
    query: str | None
    interests: list[str]
    library: list[str]
    reading: list[str]
    exclude: set[str]
    series: list[tuple[str, int]]     # (title, chapters) of the long serials found on disk
    tendencies: list[dict]
    candidates: list[dict]
    books: list[dict]
    model_failures: list[str]


PROFILE_SYSTEM = """你是讀書夥伴，替使用者整理「接下來可能想讀什麼」。只根據提供的資料判斷，不要編造。
輸出 1–4 個閱讀傾向。每個傾向有一個簡短名稱和 3–8 個搜尋關鍵詞：關鍵詞會拿去比對使用者電腦裡的檔名和資料夾名稱，
所以要寫檔名裡可能真的出現的詞，同一個概念的中文與英文都要列（例如 強化學習、reinforcement、RL、Q-learning）。"""

PICK_SYSTEM = """你是讀書夥伴，要從候選檔案中挑出最多 8 本最可能是使用者想讀的書，擺上首頁書櫃。
優先挑：完整的書或講義、和閱讀傾向最相關的、不重複的主題。避開：發票、表單、合約、履歷、說明書這類不是拿來讀的檔案。
用候選清單前面的編號回答，至少挑 4 本。每本附一句 20 字內的理由，用溫和的口吻直接對使用者說。"""


def _profile_prompt(s: ScoutState, folders: list[dict]) -> str:
    parts = []
    if s.get("query"):
        parts.append(f"使用者剛剛輸入想讀的內容（最優先，第一個傾向必須對應它）：{s['query']}")
    if s.get("interests"):
        parts.append("使用者以前輸入過的閱讀需求：" + "、".join(s["interests"][:8]))
    if s.get("library"):
        parts.append("已經匯入的教材：" + "、".join(s["library"][:15]))
    if s.get("reading"):
        parts.append("最近在讀：" + "、".join(s["reading"][:8]))
    if s.get("series"):
        parts.append("電腦裡收藏的長篇連載（章節數，代表使用者真的在追的書；請把它們的類型列為一個傾向，"
                     "例如 網路小說、奇幻小說，關鍵詞用這些書名裡的詞）：" + "、".join(f"{n}（{k} 章）" for n, k in s["series"][:8]))
    if folders:
        parts.append("電腦裡放最多可讀檔案的資料夾（資料夾｜數量｜幾個檔名）：\n" + "\n".join(
            f"- {f['folder']}｜{f['count']}｜{'；'.join(f['samples'])}" for f in folders[:16]))
    return "\n\n".join(parts) or "（沒有任何資料，請給出一般大學生常讀的主題）"


def _pick_prompt(s: ScoutState) -> str:
    tend = "、".join(t["label"] for t in s.get("tendencies") or [])
    def size(c: dict) -> str:
        return f"｜{c['pages']} 頁" if c.get("pages") else f"｜連載 {c['parts']} 章" if c.get("parts") else ""
    rows = [f"{k}. {c['title']}｜{c['kind']}{size(c)}｜資料夾 {c['folder']}" for k, c in enumerate(s["candidates"], 1)]
    head = f"使用者輸入：{s['query']}\n" if s.get("query") else ""
    return f"{head}閱讀傾向：{tend or '（未知）'}\n\n候選檔案（編號. 標題｜格式｜大小｜資料夾）：\n" + "\n".join(rows)


def _reason(text: str, title: str) -> str:
    """A reason that only echoes the candidate row (title, format, pages) says nothing; it is dropped."""
    text = text.strip()
    return "" if "｜" in text or title[:12] in text else text


class ShelfScout:
    def __init__(self, index: ShelfIndex, meta: BookMeta, runtime: ModelRuntime):
        self.index, self.meta, self.runtime = index, meta, runtime
        g = StateGraph(ScoutState)
        g.add_node("profile", self._profile)
        g.add_node("search", self._search)
        g.add_node("pick", self._pick)
        g.add_edge(START, "profile")
        g.add_edge("profile", "search")
        g.add_conditional_edges("search", lambda s: "pick" if s.get("candidates") else END, {"pick": "pick", END: END})
        g.add_edge("pick", END)
        self.graph = g.compile()

    async def run(self, state: ScoutState) -> ScoutState:
        return await self.graph.ainvoke({"model_failures": [], **state})

    async def _ask(self, system: str, prompt: str, schema: type[BaseModel], meta: dict[str, Any], json_schema: dict | None = None) -> BaseModel:
        chat = self.runtime.chat_model(min_max_tokens=700)
        bound = chat.bind(json_schema=json_schema or schema.model_json_schema(), meta=meta)
        msg = await asyncio.wait_for(bound.ainvoke([SystemMessage(system), HumanMessage(prompt)]), CALL_TIMEOUT_S)
        return parse_model_json(str(msg.content), schema)

    async def _profile(self, s: ScoutState) -> ScoutState:
        try:
            p = await self._ask(PROFILE_SYSTEM, _profile_prompt(s, self.index.folders()), Profile,
                                {"step": "shelf_profile", "query": s.get("query"), "interests": s.get("interests") or [],
                                 "library": s.get("library") or []})
            tendencies = [{"label": t.label.strip(), "keywords": [k.strip() for k in t.keywords if k.strip()]} for t in p.tendencies]
        except Exception as e:   # no model / bad output / timeout → the user's own words are the tendencies
            s["model_failures"].append(f"profile: {e}")
            tendencies = [{"label": w[:16], "keywords": keywords_of(w)} for w in ([s["query"]] if s.get("query") else []) + (s.get("interests") or [])[:3]]
        if s.get("query") and not any(set(keywords_of(s["query"])) & {k.lower() for k in t["keywords"]} for t in tendencies[:1]):
            tendencies.insert(0, {"label": s["query"][:16], "keywords": keywords_of(s["query"])})
        unique, seen = [], set()
        for t in tendencies:   # a small model may repeat a label; the first one stays
            if t["keywords"] and t["label"].lower() not in seen:
                seen.add(t["label"].lower())
                unique.append(t)
        return {"tendencies": unique[:4], "model_failures": s["model_failures"]}

    async def _search(self, s: ScoutState) -> ScoutState:
        return {"candidates": await asyncio.to_thread(self._candidates, s)}

    def _candidates(self, s: ScoutState) -> list[dict]:
        exclude, known = s.get("exclude") or set(), self.meta.known()
        lists = [[(e, t["label"]) for e, _ in rank(self.index.entries, [k.lower() for k in t["keywords"]], exclude=exclude,
                                                   limit=CANDIDATES, known=known)]
                 for t in s.get("tendencies") or []]
        if not s.get("query"):   # without a request, books that simply look like books fill the remaining places
            lists.append([(e, None) for e, _ in rank(self.index.entries, [], exclude=exclude, limit=CANDIDATES, known=known)])
        out, seen = [], set()
        for row in range(CANDIDATES):   # round-robin: every tendency gets its best books in before anyone's tenth
            for lst in lists:
                if row < len(lst) and lst[row][0].id not in seen and len(out) < CANDIDATES:
                    seen.add(lst[row][0].id)
                    out.append(self.meta.book(lst[row][0], tendency=lst[row][1]))
        self.meta.save()
        return out

    async def _pick(self, s: ScoutState) -> ScoutState:
        cands = s["candidates"]
        chosen: list[dict] = []
        # small models answer numbers far more reliably than ids; the schema bounds them and asks for a few at least
        schema = Picks.model_json_schema()
        schema["properties"]["picks"].update(minItems=min(MIN_PICKS, len(cands)), maxItems=min(SHELF_SIZE, len(cands)))
        schema["$defs"]["Pick"]["properties"]["n"].update(minimum=1, maximum=len(cands))
        try:
            p = await self._ask(PICK_SYSTEM, _pick_prompt(s), Picks, {"step": "shelf_pick", "candidates": len(cands)}, schema)
            for pk in p.picks:
                if 1 <= pk.n <= len(cands) and all(c["id"] != cands[pk.n - 1]["id"] for c in chosen):
                    chosen.append({**cands[pk.n - 1], "reason": _reason(pk.reason, cands[pk.n - 1]["title"])})
        except Exception as e:
            s["model_failures"].append(f"pick: {e}")
        # a model that chose a few has also judged the rest unfit; only when it failed (or chose almost nothing)
        # does keyword order fill the shelf up to a pleasant length
        want = len(chosen) if len(chosen) >= MIN_PICKS else min(6, len(cands))
        for c in cands:
            if len(chosen) >= want:
                break
            if all(x["id"] != c["id"] for x in chosen):
                chosen.append({**c, "reason": ""})
        return {"books": chosen, "model_failures": s["model_failures"]}


def log_line(state: ScoutState) -> str:
    return json.dumps({"tendencies": [t["label"] for t in state.get("tendencies") or []],
                       "books": len(state.get("books") or []), "model_failures": state.get("model_failures") or []},
                      ensure_ascii=False)
