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
    heading_level: int | None = None     # markdown heading depth (1 = #); None for body blocks / unknown (PDF)
    structure_confidence: Literal["high", "low"] | None = None   # headings: from markup (high) or a layout guess (low)
    meta: dict[str, Any] = Field(default_factory=dict)            # extraction evidence (R5: fonts, bbox, render refs)


class SourceUnit(BaseModel):
    unit_id: str
    ordinal: int
    block_ids: list[str] = Field(min_length=1)
    title: str | None = None
    context_refs: list[str] = Field(default_factory=list)   # read-only neighbours shown to the planner
    boundary_reason: str = ""
    segmentation_version: str
    chunks: list["UnitChunk"] = Field(default_factory=list)   # 區塊: the paragraph-sized pieces a guide is written for


class UnitChunk(BaseModel):
    """One 區塊 of a unit (主題): about a paragraph of source blocks, with the outline the planner wrote for it. The
    outline is a scope hint for the guide, never content."""
    block_ids: list[str] = Field(min_length=1)
    topic: str = ""
    aspect: str = ""
    summary: str = ""


class ReadingPart(BaseModel):
    """A reading / review product: at most one formal third-level section of the book (or one independent topic
    when the source has no such structure). Not a model batch: model windows are planned separately."""
    part_id: str
    index: int
    unit_ids: list[str]
    estimated_tokens: int
    estimation_method: str
    section_path: list[str] = Field(default_factory=list)
    boundary_reason: str = ""
    structure_confidence: Literal["high", "low"] = "high"


class SegmentationPlan(BaseModel):
    units: list[SourceUnit]
    warnings: list[str] = Field(default_factory=list)
    planning_window_id: str | None = None
    planner: str = "rules"                      # model | rules | mixed
    windows: list[dict[str, Any]] = Field(default_factory=list)   # model batches: owned/context ids, planner, notes
    relations: list[dict[str, Any]] = Field(default_factory=list) # protected relations kept across units (size cap)
    skipped: list[dict[str, Any]] = Field(default_factory=list)   # 區塊 left out as not needing a guide: block_ids, role, why


# -- what the model returns for one planning window (local labels, never text) --------------------

class PlannedUnit(BaseModel):
    blocks: list[str] = Field(min_length=1)      # local labels of owned blocks, e.g. ["B3", "B4"]
    title: str | None = None
    reason: str = ""                              # short boundary reason


class ParagraphTopic(BaseModel):
    topic: str = Field(min_length=1)   # what the paragraph mainly introduces ("…總覽" for an overview of several items)
    aspect: str = ""                   # which side of that topic it covers
    summary: str = ""                  # segment_outline: what the paragraph says, in one sentence
    # segment_outline: content to guide, or something a reader does not need guided (a table of contents, a
    # chapter summary / review, other front or back matter); when unsure, content
    role: Literal["content", "toc", "summary", "other"] = "content"


class TopicSummary(BaseModel):
    """segment_topic_summary output: one topic / aspect per paragraph of the unit, in order."""
    paragraphs: list[ParagraphTopic] = Field(min_length=1)


class Outline(BaseModel):
    """segment_outline output: topic / aspect / one-sentence summary of every 區塊, in order."""
    paragraphs: list[ParagraphTopic] = Field(min_length=1)


class TopicStarts(BaseModel):
    """segment_topic output: the paragraph numbers (1-based) where a new topic begins; empty = one topic."""
    new_topic_at: list[int] = Field(default_factory=list)


class WindowPlan(BaseModel):
    units: list[PlannedUnit] = Field(min_length=1)
