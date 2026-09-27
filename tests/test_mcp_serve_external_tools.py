"""
Tests for mcp_tools_bridge — exposing internal tools (read_file, browser,
etc.) as MCP tools, opt-in via mcp_serve.create_mcp_server(expose_tools=...).

Mirrors the patterns in test_mcp_serve.py: an E2E server built via
create_mcp_server(), tool calls made through FastMCP's tool manager.
"""

import asyncio
import json

import pytest


@pytest.fixture
def _event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def _run_tool(server, name, args=None):
    from mcp.server.mcpserver import Context  # mcp 2.0: call_tool requires a context

    result = asyncio.get_event_loop().run_until_complete(
        server._tool_manager.call_tool(name, args or {}, Context(mcp_server=server))
    )
    return json.loads(result) if isinstance(result, str) else result


@pytest.fixture
def mcp_server_default(monkeypatch, tmp_path):
    """create_mcp_server() with no expose_tools — today's default behavior."""
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    import mcp_serve
    monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_serve, "_load_channel_directory", lambda: {})
    bridge = mcp_serve.EventBridge()
    return mcp_serve.create_mcp_server(event_bridge=bridge)


@pytest.fixture
def mcp_server_external(monkeypatch, tmp_path):
    """create_mcp_server(expose_tools="hermes-mcp-external")."""
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    import mcp_serve
    monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_serve, "_load_channel_directory", lambda: {})
    bridge = mcp_serve.EventBridge()
    server = mcp_serve.create_mcp_server(event_bridge=bridge, expose_tools="hermes-mcp-external")
    return server, bridge


class TestDefaultIsUnchanged:
    """No expose_tools => identical surface to the pre-existing chat bridge."""

    def test_only_ten_bridge_tools(self, mcp_server_default, _event_loop):
        tool_names = {t.name for t in mcp_server_default._tool_manager.list_tools()}
        expected = {
            "conversations_list", "conversation_get", "messages_read",
            "attachments_fetch", "events_poll", "events_wait",
            "messages_send", "channels_list",
            "permissions_list_open", "permissions_respond",
        }
        assert tool_names == expected


class TestExternalToolExposure:
    def test_safe_tools_registered(self, mcp_server_external, _event_loop):
        server, _bridge = mcp_server_external
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        # read_file has no external dependency (API key, browser), so it
        # must always be present when hermes-mcp-external is exposed.
        assert "read_file" in tool_names
        # Bridge tools must still be present alongside the external ones.
        assert "conversations_list" in tool_names
        # Dangerous tools must never appear in the safe toolset.
        assert "terminal" not in tool_names
        assert "execute_code" not in tool_names
        assert "write_file" not in tool_names

    def test_read_file_dispatches_for_real(self, mcp_server_external, _event_loop, tmp_path):
        server, _bridge = mcp_server_external
        target = tmp_path / "hello.txt"
        target.write_text("hello world\n")

        result = _run_tool(server, "read_file", {"path": str(target)})
        assert "hello world" in result["content"]

    def test_unknown_toolset_registers_nothing_extra(self, monkeypatch, tmp_path, _event_loop):
        pytest.importorskip("mcp", reason="MCP SDK not installed")
        import mcp_serve
        monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr(mcp_serve, "_load_channel_directory", lambda: {})
        bridge = mcp_serve.EventBridge()
        server = mcp_serve.create_mcp_server(event_bridge=bridge, expose_tools="does-not-exist")
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        assert tool_names == {
            "conversations_list", "conversation_get", "messages_read",
            "attachments_fetch", "events_poll", "events_wait",
            "messages_send", "channels_list",
            "permissions_list_open", "permissions_respond",
        }


class TestDangerousApprovalBridge:
    def test_dangerous_command_blocks_until_resolved_via_permissions_respond(
        self, monkeypatch, tmp_path, _event_loop
    ):
        pytest.importorskip("mcp", reason="MCP SDK not installed")
        import mcp_serve
        monkeypatch.setenv("HERMES_GATEWAY_SESSION", "")
        monkeypatch.setenv("HERMES_EXEC_ASK", "")
        monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr(mcp_serve, "_load_channel_directory", lambda: {})

        bridge = mcp_serve.EventBridge()
        server = mcp_serve.create_mcp_server(
            event_bridge=bridge, expose_tools="hermes-mcp-external-dangerous"
        )
        assert "terminal" in {t.name for t in server._tool_manager.list_tools()}

        from tools.approval import _gateway_notify_cbs, _gateway_queues
        from tools.approval_gateway_wait import _ApprovalEntry

        session_key = next(iter(_gateway_notify_cbs.keys()))
        cb = _gateway_notify_cbs[session_key]

        # Simulate tools/approval.py raising a dangerous-command approval,
        # as check_all_command_guards() does from inside terminal_tool.
        entry = _ApprovalEntry({"command": "rm -rf build/"})
        _gateway_queues.setdefault(session_key, []).append(entry)
        cb({"command": "rm -rf build/", "description": "recursive delete"})

        pending = _run_tool(server, "permissions_list_open")
        assert pending["count"] == 1
        approval_id = pending["approvals"][0]["id"]
        assert pending["approvals"][0]["session_key"] == session_key

        result = _run_tool(server, "permissions_respond", {"id": approval_id, "decision": "deny"})
        assert result["resolved"] is True
        assert entry.result == "deny"
