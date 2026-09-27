"""Per-agent policy on the MCP and API network surfaces (agent_policy.py).

Why: until F2 both listeners accepted one shared token, so every caller was "the
operator". These tests drive the real PolicyStore on a throwaway state.db, the
real MCP bearer middleware inside a real Starlette app, and the real API server
middleware inside a real aiohttp app. No doubles.
@see agent_policy.py, mcp_tools_bridge.build_bearer_auth_middleware,
     gateway/platforms/api_server.py (APIServerAdapter._apply_agent_policy)
"""
import hashlib
import json
import sqlite3

import pytest
from aiohttp.test_utils import TestClient as TestClient_, TestServer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_policy import PolicyStore
from mcp_tools_bridge import build_bearer_auth_middleware

OPERATOR = "operator-shared-token"


@pytest.fixture()
def store(tmp_path):
    return PolicyStore(tmp_path / "state.db")


def _tool_call(name: str, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": {}}}


def _mcp_client(store: PolicyStore) -> TestClient:
    async def echo(request: Request):
        body = await request.json()
        return JSONResponse({"reached": body.get("params", {}).get("name") or body.get("method")})

    app = Starlette(
        routes=[Route("/mcp", echo, methods=["POST"])],
        middleware=[Middleware(build_bearer_auth_middleware(OPERATOR, store=store))],
    )
    return TestClient(app)


# ── PolicyStore ─────────────────────────────────────────────────────────────

def test_token_is_stored_only_as_hash(store):
    token = store.add_agent("metacognition", tools=["*"], rate_per_min=10, max_concurrent=2)
    rows = sqlite3.connect(store.db_path).execute("select token_sha256 from agent_policies").fetchall()
    assert rows == [(hashlib.sha256(token.encode()).hexdigest(),)]
    assert store.authenticate(token).agent_id == "metacognition"
    assert store.authenticate("not-a-token") is None


def test_disabled_agent_cannot_authenticate(store):
    token = store.add_agent("reviewer", tools=["*"])
    store.set_enabled("reviewer", False)
    assert store.authenticate(token) is None


def test_tool_patterns(store):
    policy = store.authenticate(store.add_agent("reader", tools=["messages_*", "conversations_list"]))
    assert policy.allows("messages_read")
    assert policy.allows("conversations_list")
    assert not policy.allows("terminal")


def test_rate_window_blocks_after_quota_and_resets(store):
    policy = store.authenticate(store.add_agent("burst", tools=["*"], rate_per_min=2))
    t0 = 1_800_000_000.0  # start of a minute window
    assert store.take_rate_slot(policy, now=t0) is None
    assert store.take_rate_slot(policy, now=t0 + 1) is None
    wait = store.take_rate_slot(policy, now=t0 + 10)
    assert wait is not None and 49 <= wait <= 50
    assert store.take_rate_slot(policy, now=t0 + 60) is None


# ── MCP surface ─────────────────────────────────────────────────────────────

def test_mcp_operator_token_keeps_working(store):
    r = _mcp_client(store).post("/mcp", json=_tool_call("terminal"), headers={"Authorization": f"Bearer {OPERATOR}"})
    assert r.status_code == 200 and r.json() == {"reached": "terminal"}


def test_mcp_agent_allowed_tool_reaches_server_with_body_intact(store):
    token = store.add_agent("reader", tools=["messages_*"])
    r = _mcp_client(store).post("/mcp", json=_tool_call("messages_read"), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json() == {"reached": "messages_read"}


def test_mcp_forbidden_tool_is_403_and_recorded(store):
    token = store.add_agent("reader", tools=["messages_*"])
    r = _mcp_client(store).post("/mcp", json=_tool_call("terminal"), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert r.json()["tool"] == "terminal"
    denials = store.recent_denials()
    assert [(d["agent_id"], d["surface"], d["reason"]) for d in denials] == [("reader", "mcp", "forbidden_tool")]


def test_mcp_rate_limit_is_429_with_retry_after(store):
    token = store.add_agent("burst", tools=["*"], rate_per_min=1)
    client, headers = _mcp_client(store), {"Authorization": f"Bearer {token}"}
    assert client.post("/mcp", json=_tool_call("messages_read"), headers=headers).status_code == 200
    r = client.post("/mcp", json=_tool_call("messages_read", 2), headers=headers)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert store.recent_denials()[0]["reason"] == "rate_limited"


def test_mcp_non_tool_methods_do_not_consume_quota(store):
    token = store.add_agent("burst", tools=["*"], rate_per_min=1)
    client, headers = _mcp_client(store), {"Authorization": f"Bearer {token}"}
    for rid in range(3):
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": "tools/list"}, headers=headers).status_code == 200
    assert client.post("/mcp", json=_tool_call("messages_read"), headers=headers).status_code == 200


def test_mcp_unknown_token_is_401(store):
    r = _mcp_client(store).post("/mcp", json=_tool_call("messages_read"), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# ── API surface ─────────────────────────────────────────────────────────────

def _api_app(store):
    from aiohttp import web
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(extra={"key": OPERATOR}))
    adapter._policy_store = store

    async def protected(request):
        err = adapter._check_auth(request)
        return err if err is not None else web.json_response({"agent": request.get("agent_id", "operator")})

    app = web.Application(middlewares=[adapter.agent_policy_middleware()])
    app.router.add_get("/v1/models", protected)
    return app


@pytest.mark.asyncio
async def test_api_agent_token_authenticates(store):
    token = store.add_agent("metacognition", tools=["*"])
    async with TestClient_(TestServer(_api_app(store))) as cli:
        r = await cli.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        assert r.status == 200 and await r.json() == {"agent": "metacognition"}


@pytest.mark.asyncio
async def test_api_operator_and_unknown_tokens_unchanged(store):
    async with TestClient_(TestServer(_api_app(store))) as cli:
        ok = await cli.get("/v1/models", headers={"Authorization": f"Bearer {OPERATOR}"})
        bad = await cli.get("/v1/models", headers={"Authorization": "Bearer nope"})
        assert ok.status == 200 and await ok.json() == {"agent": "operator"}
        assert bad.status == 401


@pytest.mark.asyncio
async def test_api_rate_limit_is_429_with_retry_after(store):
    token = store.add_agent("burst", tools=["*"], rate_per_min=1)
    headers = {"Authorization": f"Bearer {token}"}
    async with TestClient_(TestServer(_api_app(store))) as cli:
        assert (await cli.get("/v1/models", headers=headers)).status == 200
        r = await cli.get("/v1/models", headers=headers)
        assert r.status == 429
        assert 1 <= int(r.headers["Retry-After"]) <= 60
        body = json.loads(await r.text())
        assert body["error"]["code"] == "rate_limited"
        assert store.recent_denials()[0]["surface"] == "api"


# ── Changing an agent's tools must not rotate its token (clients hold a copy) ──

def test_set_tools_changes_permissions_without_rotating_token(store):
    token = store.add_agent("reader", tools=["messages_*"])
    store.set_tools("reader", ["find_files"])
    policy = store.authenticate(token)
    assert policy is not None
    assert policy.allows("find_files")
    assert not policy.allows("messages_read")


def test_set_tools_unknown_agent_is_an_error(store):
    with pytest.raises(KeyError):
        store.set_tools("ghost", ["find_files"])
