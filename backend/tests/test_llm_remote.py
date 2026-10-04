"""Remote inference is a development option: only the model call goes to the remote Ollama, only when explicitly
allowed, with the proxy token taken from the environment; eval runs record where the model ran."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.llm import ollama
from app.llm.base import LLMError

REMOTE = "https://abc.trycloudflare.com"


@pytest.fixture
def seen(monkeypatch):
    """Route the adapter's HTTP client to an in-memory Ollama and record each request."""
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": "{}"}, "prompt_eval_count": 3, "eval_count": 1})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3-vl:4b", "digest": "sha256:abc"}]})
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.35"})
        return httpx.Response(200, json={"model_info": {"qwen3vl.context_length": 262144}, "capabilities": ["vision"]})

    real = httpx.AsyncClient
    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    for k in ("MENTOR_OLLAMA_TOKEN", "MENTOR_ALLOW_REMOTE_MODEL", "MENTOR_OLLAMA_SEED"):
        monkeypatch.delenv(k, raising=False)
    return requests


def chat(host: str):
    b = ollama.OllamaBackend(host, "qwen3-vl:4b")
    return asyncio.run(b.generate([{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=8, context_tokens=1024))


def test_only_internet_hosts_count_as_remote():
    local = ["http://127.0.0.1:11434", "http://localhost:11434", "127.0.0.1:11434", "http://[::1]:11434",
             "http://192.168.1.5:11434", "http://10.0.0.2:11434", "http://ollama:11434", "http://studio.local:11434"]
    assert not any(ollama.is_remote(h) for h in local)
    assert all(ollama.is_remote(h) for h in [REMOTE, "http://8.8.8.8:11434", "https://ollama.example.com"])


def test_remote_host_is_refused_without_explicit_permission(seen):
    with pytest.raises(LLMError, match="MENTOR_ALLOW_REMOTE_MODEL"):
        chat(REMOTE)
    assert seen == []                                            # nothing was sent
    b = ollama.OllamaBackend(REMOTE, "qwen3-vl:4b")
    assert asyncio.run(b.capabilities()).source == "ollama:unreachable" and seen == []
    h = asyncio.run(b.health())
    assert not h["ok"] and "MENTOR_ALLOW_REMOTE_MODEL" in h["detail"] and seen == []
    assert asyncio.run(b.model_digest()) is None and seen == []


def test_allowed_remote_call_carries_token_and_seed(seen, monkeypatch):
    monkeypatch.setenv("MENTOR_ALLOW_REMOTE_MODEL", "1")
    monkeypatch.setenv("MENTOR_OLLAMA_TOKEN", "s3cret")
    monkeypatch.setenv("MENTOR_OLLAMA_SEED", "42")
    chat(REMOTE)
    req = seen[-1]
    assert req.url.path == "/api/chat" and req.headers["Authorization"] == "Bearer s3cret"
    assert b'"seed":42' in req.content.replace(b" ", b"")
    b = ollama.OllamaBackend(REMOTE, "qwen3-vl:4b")
    assert asyncio.run(b.model_digest()) == "sha256:abc"
    assert all(r.headers["Authorization"] == "Bearer s3cret" for r in seen)


def test_local_call_sends_no_token_or_seed_unless_set(seen):
    chat("http://127.0.0.1:11434")
    req = seen[-1]
    assert "Authorization" not in req.headers and b'"seed"' not in req.content


def load_eval_script():
    spec = importlib.util.spec_from_file_location("eval_reading_script", Path(__file__).parent.parent / "scripts" / "eval_reading.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_eval_provenance_separates_quality_from_latency_acceptance(seen, monkeypatch):
    """A remote run of the same model (same digest as local) is accepted for quality, never for latency; a remote
    model that cannot be matched to the local one is only a development reference."""
    er = load_eval_script()
    monkeypatch.setenv("MENTOR_ALLOW_REMOTE_MODEL", "1")
    monkeypatch.setenv("MENTOR_OLLAMA_SEED", "7")

    def prov(host, jobs, backend="ollama", local_host="http://127.0.0.1:11434"):
        args = SimpleNamespace(backend=backend, model="qwen3-vl:4b", host=host, model_path="", device="", temperature=0.2, jobs=jobs)
        cfg = er.model_config(args)
        return er.provenance(args, cfg, er.build_backend(cfg), local_host=local_host)

    from evals import reading_eval as ev
    p = prov(REMOTE, 3)                      # the in-memory Ollama answers both hosts with the same digest
    assert p["run_class"] == "remote-quality" and p["digest_match"] and p["quality_acceptable"]
    assert p["seed"] == 7 and not p["latency_comparable"]
    assert any("可作為品質驗收" in x and "耗時不能" in x for x in ev.provenance_lines(p))
    loc = prov("http://127.0.0.1:11434", 1)
    assert loc["run_class"] == "local-acceptance" and loc["latency_comparable"] and loc["quality_acceptable"]
    assert prov("http://127.0.0.1:11434", 2)["run_class"] == "local-parallel"
    assert not prov("", 1, backend="mock")["quality_acceptable"]


def test_remote_run_with_a_different_model_is_not_accepted(monkeypatch):
    er = load_eval_script()
    monkeypatch.setenv("MENTOR_ALLOW_REMOTE_MODEL", "1")
    digests = {"abc.trycloudflare.com": "sha256:remote", "127.0.0.1": "sha256:local"}
    real = httpx.AsyncClient
    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"models": [{"name": "qwen3-vl:4b", "digest": digests[req.url.host]}]})), **kw))
    args = SimpleNamespace(backend="ollama", model="qwen3-vl:4b", host=REMOTE, model_path="", device="", temperature=0.2, jobs=1)
    cfg = er.model_config(args)
    p = er.provenance(args, cfg, er.build_backend(cfg))
    assert p["run_class"] == "remote-unverified" and p["digest_match"] is False and not p["quality_acceptable"]
    from evals import reading_eval as ev
    assert any("不能驗收" in x for x in ev.provenance_lines(p))


def load_proxy():
    spec = importlib.util.spec_from_file_location("ollama_proxy", Path(__file__).parent.parent / "scripts" / "colab" / "ollama_proxy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_colab_proxy_checks_token_and_forwards_only_the_adapter_endpoints():
    from starlette.testclient import TestClient
    proxy = load_proxy()
    upstream: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        upstream.append(req.url.path)
        if req.url.path == "/api/chat":
            return httpx.Response(500, json={"error": "out of memory"}) if b"boom" in req.content else \
                httpx.Response(200, json={"message": {"content": "{}"}})
        return httpx.Response(200, json={"version": "0.35"})

    token = "t" * 32
    c = TestClient(proxy.build_app(token, "http://up", transport=httpx.MockTransport(handler)))
    auth = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/version").status_code == 401
    assert c.get("/api/version", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/api/pull", headers=auth, json={"model": "x"}).status_code == 404
    assert c.delete("/api/delete", headers=auth).status_code == 404
    assert upstream == []                                            # nothing reached Ollama
    assert c.get("/api/version", headers=auth).json() == {"version": "0.35"}
    assert c.post("/api/chat", headers=auth, json={"m": 1}).json() == {"message": {"content": "{}"}}
    late = c.post("/api/chat", headers=auth, json={"m": "boom"})
    assert late.status_code == 200 and "out of memory" in late.json()["error"]   # surfaced in the body


def test_colab_proxy_sends_keepalive_spaces_while_the_model_works():
    from starlette.testclient import TestClient
    proxy = load_proxy()
    proxy.HEARTBEAT_S = 0.05

    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"message": {"content": "{}"}})

    token = "t" * 32
    c = TestClient(proxy.build_app(token, "http://up", transport=httpx.MockTransport(slow)))
    r = c.post("/api/chat", headers={"Authorization": f"Bearer {token}"}, json={})
    assert r.content.startswith(b" ") and r.json() == {"message": {"content": "{}"}}


def test_local_digest_falls_back_to_the_manifest_file(tmp_path, monkeypatch):
    """Without a running local server the local model is identified from its manifest (sha256 of the file)."""
    import hashlib
    man = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen3-vl" / "4b"
    man.parent.mkdir(parents=True)
    man.write_bytes(b'{"layers": []}')
    want = hashlib.sha256(b'{"layers": []}').hexdigest()
    assert ollama.manifest_digest("qwen3-vl:4b", str(tmp_path)) == want
    assert ollama.manifest_digest("qwen3-vl:8b", str(tmp_path)) is None

    er = load_eval_script()
    monkeypatch.setenv("MENTOR_ALLOW_REMOTE_MODEL", "1")
    real = httpx.AsyncClient

    def handler(req):
        if req.url.host == "127.0.0.1":
            raise httpx.ConnectError("local ollama not running")
        return httpx.Response(200, json={"models": [{"name": "qwen3-vl:4b", "digest": want}]})
    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    args = SimpleNamespace(backend="ollama", model="qwen3-vl:4b", host=REMOTE, model_path="", device="", temperature=0.2, jobs=1)
    cfg = er.model_config(args)
    p = er.provenance(args, cfg, er.build_backend(cfg), models_dir=str(tmp_path))
    assert p["run_class"] == "remote-quality" and p["local_model_digest"] == want

