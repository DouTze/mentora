"""Contracts of the source model (plan §4.1).

SourceBlock — smallest traceable piece of extracted text; its text is never rewritten by a model.
SourceUnit  — one or more consecutive blocks taught together (= a legacy quote).
ReadingPart — consecutive units read in one context window (= a legacy part).
SegmentationPlan — the units of a whole range plus warnings."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

BlockKind = Literal["heading", "paragraph", "list_item", "formula", "code", "table", "figure", "quote"]


class Locator(BaseModel):
    unit: Literal["page", "line"]
    start: int                      # page / line number (1-based) where the block starts
    end: int                        # … and ends (inclusive)
    para: int | None = None         # paragraph index on the page (PDF), 1-based
    paras: int | None = None        # paragraphs on that page
    char_start: int = 0             # offsets into the versioned extraction text of `start` (page text / line);
    char_end: int = 0               # they do not claim to map to PDF bytes


class SourceBlock(BaseModel):
    block_id: str
    material_snapshot_id: str
    ordinal: int                    # reading order within the snapshot, 0-based
    kind: BlockKind
    text: str
    locator: Locator
    text_hash: str
    extraction_version: str
    parent_block_id: str | None = None   # set on pieces cut from an oversize block
    piece: int | None = None             # 1-based piece number under parent_block_id
    continues: bool = False              # text continues in the next block (page break mid-sentence, cut piece)


class SourceUnit(BaseModel):
    unit_id: str
    ordinal: int
    block_ids: list[str] = Field(min_length=1)
    title: str | None = None
    context_refs: list[str] = Field(default_factory=list)   # read-only neighbours shown to the planner
    boundary_reason: str = ""
    segmentation_version: str


class ReadingPart(BaseModel):
    part_id: str
    index: int
    unit_ids: list[str]
    estimated_tokens: int
    estimation_method: str


class SegmentationPlan(BaseModel):
    units: list[SourceUnit]
    warnings: list[str] = Field(default_factory=list)
    planning_window_id: str | None = None
    planner: str = "rules"                      # model | rules | mixed
    windows: list[dict[str, Any]] = Field(default_factory=list)   # per window: owned/context ids, planner, notes


# -- what the model returns for one planning window (local labels, never text) --------------------

class PlannedUnit(BaseModel):
    blocks: list[str] = Field(min_length=1)      # local labels of owned blocks, e.g. ["B3", "B4"]
    title: str | None = None
    reason: str = ""                              # short boundary reason


class WindowPlan(BaseModel):
    units: list[PlannedUnit] = Field(min_length=1)
