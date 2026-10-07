"""Pins the LangGraph behaviour the workflow relies on (P1 spike, langgraph 1.2.x, file SQLite, fresh processes):
- with durability="sync" a hard exit inside a subgraph node resumes at that node, earlier nodes are not re-run;
- resuming the same event twice is NOT de-duplicated by the framework → the runner must reconcile
  (last_applied_event_id) before resuming."""
import json
import subprocess
import sys
from pathlib import Path

from conftest import child_env

HELPER = Path(__file__).parent / "helpers" / "graph_spike.py"


def run(tmp_path, cmd, crash=None):
    env = child_env(**({"CRASH": crash} if crash else {}))
    p = subprocess.run([sys.executable, str(HELPER), str(tmp_path / "cp.sqlite3"), str(tmp_path / "log"), cmd],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    return p.returncode, (json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else p.stderr)


def calls(tmp_path):
    return (tmp_path / "log").read_text().split()


def test_hard_exit_inside_subgraph_resumes_at_the_failed_node(tmp_path):
    code, _ = run(tmp_path, "start", crash="draft")
    assert code == 3
    code, out = run(tmp_path, "continue")
    assert code == 0 and out["next"] == ["wait"] and out["steps"] == ["analyze", "draft", "review"]
    assert calls(tmp_path) == ["analyze", "draft", "draft", "review"]   # analyze ran once


def test_duplicate_resume_is_applied_twice_by_the_framework(tmp_path):
    run(tmp_path, "start")
    run(tmp_path, "resume:e1")
    code, out = run(tmp_path, "resume:e1")
    assert code == 0 and calls(tmp_path).count("apply") == 2
