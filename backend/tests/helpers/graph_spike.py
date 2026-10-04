"""Run in a separate process by test_graph_checkpoint.py: a parent graph with a subgraph node and a wait node.
argv: <db> <log> start|continue|resume:<id>; env CRASH=<node> hard-exits inside that node (power loss)."""
import asyncio
import json
import operator
import os
import sys
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

DB, LOG, CMD = sys.argv[1], sys.argv[2], sys.argv[3]


def log(x):
    with open(LOG, "a") as f:
        f.write(x + "\n")


class Sub(TypedDict, total=False):
    steps: Annotated[list, operator.add]


def step(name):
    async def run(state):
        log(name)
        if os.environ.get("CRASH") == name:
            os._exit(3)
        return {"steps": [name]}
    return run


sb = StateGraph(Sub)
for n in ("analyze", "draft", "review"):
    sb.add_node(n, step(n))
sb.add_edge(START, "analyze"); sb.add_edge("analyze", "draft"); sb.add_edge("draft", "review"); sb.add_edge("review", END)


class S(TypedDict, total=False):
    steps: Annotated[list, operator.add]
    event: dict


async def wait(state):
    ev = interrupt({"waiting": "student"})
    return {"event": ev}


g = StateGraph(S)
g.add_node("guide", sb.compile()); g.add_node("wait", wait); g.add_node("apply", step("apply"))
g.add_edge(START, "guide"); g.add_edge("guide", "wait"); g.add_edge("wait", "apply"); g.add_edge("apply", "wait")


async def main():
    async with AsyncSqliteSaver.from_conn_string(DB) as cp:
        app = g.compile(checkpointer=cp)
        cfg = {"configurable": {"thread_id": "t"}}
        arg = {} if CMD == "start" else None if CMD == "continue" else Command(resume={"id": CMD.split(":")[1]})
        await app.ainvoke(arg, cfg, durability="sync")
        snap = await app.aget_state(cfg)
        print(json.dumps({"next": list(snap.next), "steps": snap.values.get("steps", [])}))


asyncio.run(main())
