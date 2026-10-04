"""LangGraph workflows (MENTOR_WORKFLOW=langgraph)."""
from __future__ import annotations


async def invoke(graph, value, config: dict):
    """Run a compiled graph. With a checkpointer every step is written synchronously (a hard exit loses at most the
    running node); langgraph 1.2.12 rejects durability="sync" on a graph without one, so it is only passed then."""
    if getattr(graph, "checkpointer", None):
        return await graph.ainvoke(value, config, durability="sync")
    return await graph.ainvoke(value, config)
