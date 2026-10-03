"""Read-only context tools of the guided-reading agent (plan §5.3).

Tools read a ReadingScope — the session's own range, never a path or SQL — and refuse anything outside it:
units of other ranges, guides that were not accepted, notes of parts not yet read, Q&A of other sessions. Later
units may be read to understand the current one, but reading them changes nothing about progress or the quiz."""
from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


class ScopeError(Exception):
    pass


@dataclass
class ReadingScope:
    quotes: list[dict]                       # all units of the session range (legacy quote shape with unit_id)
    current_index: int
    guides: dict[str, dict] = field(default_factory=dict)      # accepted guides only, by quote index
    compacts: dict[str, dict] = field(default_factory=dict)    # by part index
    parts: list[dict] = field(default_factory=list)
    qa: dict[str, list[dict]] = field(default_factory=dict)

    def index_of(self, unit_id: str) -> int:
        for q in self.quotes:
            if q.get("unit_id") == unit_id or q.get("id") == unit_id:
                return q["index"]
        raise ScopeError(f"{unit_id} 不在本次學習範圍")


def _fmt(q: dict) -> str:
    head = f"[{q.get('unit_id') or q['id']}｜{q['label']}" + (f"｜{q['heading']}" if q.get("heading") else "") + "]"
    return f"{head}\n{q['text']}"


class UnitIds(BaseModel):
    unit_ids: list[str] = Field(min_length=1, max_length=3, description="本次學習範圍內的單元 id")


class Neighbor(BaseModel):
    unit_id: str
    before: int = Field(default=1, ge=0, le=1)
    after: int = Field(default=0, ge=0, le=1)


class UnitId(BaseModel):
    unit_id: str


class PartId(BaseModel):
    part_id: str


class QaArgs(BaseModel):
    unit_id: str
    limit: int = Field(default=3, ge=1, le=5)


def build_tools(scope: ReadingScope) -> list[BaseTool]:
    async def read_source_units(unit_ids: list[str]) -> str:
        return "\n\n".join(_fmt(scope.quotes[scope.index_of(u)]) for u in unit_ids)

    async def read_neighbor_units(unit_id: str, before: int = 1, after: int = 0) -> str:
        i = scope.index_of(unit_id)
        idx = [j for j in range(i - before, i + after + 1) if 0 <= j < len(scope.quotes) and j != i]
        if not idx:
            return "（沒有相鄰單元）"
        return "\n\n".join(("［後文，只供理解，不屬於這一段］\n" if j > i else "") + _fmt(scope.quotes[j]) for j in idx)

    async def read_accepted_guide(unit_id: str) -> str:
        i = scope.index_of(unit_id)
        if i >= scope.current_index:
            raise ScopeError("只能讀已經讀過的段落的導讀")
        g = scope.guides.get(str(i))
        if not g:
            raise ScopeError("這一段沒有已驗收的導讀")
        return f"[{unit_id} 的導讀]\n{g['guide']}"

    async def read_compact(part_id: str) -> str:
        part = next((p for p in scope.parts if p.get("part_id") == part_id or str(p["index"]) == part_id), None)
        if part is None:
            raise ScopeError(f"{part_id} 不在本次學習範圍")
        if part["quote_to"] >= scope.current_index:
            raise ScopeError("這個部分還沒讀完，沒有筆記")
        c = scope.compacts.get(str(part["index"]))
        if not c:
            raise ScopeError("這個部分沒有筆記")
        return c.get("markdown") or c.get("summary", "")

    async def read_qa_history(unit_id: str, limit: int = 3) -> str:
        i = scope.index_of(unit_id)
        if i > scope.current_index:
            raise ScopeError("只能讀已經讀到的段落的問答")
        qa = scope.qa.get(str(i), [])[-limit:]
        return "\n".join(f"學生：{x['question']}\n導師：{x['answer']}" for x in qa) or "（這一段沒有問答）"

    spec = [
        (read_source_units, "read_source_units", "讀取本次學習範圍內指定的來源單元原文（最多 3 個）", UnitIds),
        (read_neighbor_units, "read_neighbor_units", "讀取某單元的前一個或後一個單元（各最多 1 個），用來解決前後文指涉", Neighbor),
        (read_accepted_guide, "read_accepted_guide", "讀取已讀過段落的已驗收導讀", UnitId),
        (read_compact, "read_compact", "讀取已讀完部分的精簡筆記", PartId),
        (read_qa_history, "read_qa_history", "讀取某段落的學生問答（最多 5 筆）", QaArgs),
    ]
    return [StructuredTool.from_function(coroutine=f, name=n, description=d, args_schema=a) for f, n, d, a in spec]


def tools_text(tools: list[BaseTool]) -> str:
    return "\n".join(f"- {t.name}：{t.description}；參數 {list(t.args_schema.model_json_schema().get('properties', {}))}"
                     for t in tools)
