"""Bearer auth of the MCP network listener (mcp_tools_bridge.build_bearer_auth_middleware).

Why this file exists: with the token comparison mutated to ``return True`` the
whole MCP suite (84 tests) stayed green, so nothing guarded the only barrier
between the Tailscale-bound MCP port and every Hermes tool. These tests drive
the real middleware inside a real Starlette app, no doubles.
@see mcp_tools_bridge.py, mcp_serve.py (where the middleware is installed)
"""
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_tools_bridge import build_bearer_auth_middleware

TOKEN = "correct-horse-battery-staple"


def _client() -> TestClient:
    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/mcp", ok, methods=["GET", "POST"])],
        middleware=[Middleware(build_bearer_auth_middleware(TOKEN))],
    )
    return TestClient(app)


def test_correct_token_passes():
    r = _client().post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.text == "ok"


def test_wrong_token_is_rejected():
    r = _client().post("/mcp", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_missing_header_is_rejected():
    assert _client().post("/mcp").status_code == 401


def test_non_bearer_scheme_is_rejected():
    r = _client().post("/mcp", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401
