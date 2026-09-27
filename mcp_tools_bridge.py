"""
Hermes MCP Tools Bridge — expose the internal tool registry (browser, files,
web search, image generation, and optionally terminal/code execution) to
external MCP clients (Claude Code, Cursor, etc.) via ``hermes mcp serve``.

This is separate from the chat/messaging bridge in ``mcp_serve.py`` (10
tools: conversations_list, messages_send, ...). Those tools read the
gateway's session store. The tools registered here dispatch through the
*same* tool registry and dispatcher (``tools/registry.py`` +
``model_tools.handle_function_call``) used by the interactive CLI and the
messaging gateway — the exact tools an agent uses when chatting with you.

Nothing here is exposed by default. It is opt-in via config
(``mcp_server.expose_tools``), the ``HERMES_MCP_EXPOSE_TOOLS`` env var, or
the ``hermes mcp serve --expose-tools <toolset>`` CLI flag. See
``toolsets.py`` for the ``hermes-mcp-external`` (safe) and
``hermes-mcp-external-dangerous`` (terminal/execute_code/write_file/patch)
toolsets.

Dangerous tools still go through the normal approval flow: this module sets
``HERMES_GATEWAY_SESSION``/``HERMES_EXEC_ASK`` (the same flags the TUI and
messaging gateway use) and wires ``tools/approval.py``'s gateway-approval
queue to the chat bridge's ``permissions_list_open``/``permissions_respond``
tools, so a dangerous command blocks until the MCP client approves or denies
it — never a silent auto-approve.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Annotated, Any, Callable, Dict, List, Optional

from pydantic import Field

logger = logging.getLogger("hermes.mcp_tools_bridge")

# JSON-Schema "type" -> bare Python type name used to build a real function
# signature (FastMCP infers the MCP tool schema from the signature).
_JSON_TYPE_TO_PY: Dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def resolve_expose_toolset(cli_flag: Optional[str]) -> Optional[str]:
    """Resolve which toolset (if any) to expose over MCP.

    Precedence: CLI flag > HERMES_MCP_EXPOSE_TOOLS env var > config.yaml
    ``mcp_server.expose_tools``. Returns None (nothing exposed) unless one
    of these explicitly names a toolset.
    """
    if cli_flag:
        return cli_flag

    env_value = os.environ.get("HERMES_MCP_EXPOSE_TOOLS", "").strip()
    if env_value:
        return env_value

    try:
        from hermes_cli.config import load_config
        config = load_config()
        value = ((config.get("mcp_server") or {}).get("expose_tools") or "").strip()
        return value or None
    except Exception as e:
        logger.debug("Could not read mcp_server.expose_tools from config: %s", e)
        return None


def resolve_network_settings(
    transport: Optional[str],
    host: Optional[str],
    port: Optional[int],
) -> Dict[str, Any]:
    """Resolve transport/host/port for ``hermes mcp serve``.

    Precedence per field: CLI flag > env var > config.yaml ``mcp_server.*``
    > built-in default (stdio / 127.0.0.1 / 8643). Mirrors
    ``resolve_expose_toolset``'s precedence order.
    """
    try:
        from hermes_cli.config import load_config
        cfg = (load_config().get("mcp_server") or {})
    except Exception as e:
        logger.debug("Could not read mcp_server.* from config: %s", e)
        cfg = {}

    resolved_transport = (
        transport
        or os.environ.get("HERMES_MCP_TRANSPORT", "").strip()
        or cfg.get("transport")
        or "stdio"
    )
    resolved_host = (
        host
        or os.environ.get("HERMES_MCP_HOST", "").strip()
        or cfg.get("host")
        or "127.0.0.1"
    )
    resolved_port = port or os.environ.get("HERMES_MCP_PORT") or cfg.get("port") or 8643
    try:
        resolved_port = int(resolved_port)
    except (TypeError, ValueError):
        resolved_port = 8643

    return {"transport": resolved_transport, "host": resolved_host, "port": resolved_port}


def _tool_call_name(body: bytes) -> str:
    """Name of the tool in a JSON-RPC ``tools/call`` body, or "" for any other message."""
    import json
    try:
        msg = json.loads(body or b"null")
    except ValueError:
        return ""
    if isinstance(msg, dict) and msg.get("method") == "tools/call":
        return str((msg.get("params") or {}).get("name") or "")
    return ""


def build_bearer_auth_middleware(token: str, store=None):
    """Build a Starlette middleware class requiring ``Authorization: Bearer <token>``.

    The MCP SDK's streamable-http transport has no built-in authentication
    (unlike ``gateway/platforms/api_server.py``, which already checks a
    bearer token — see ``_check_auth`` there). This mirrors that pattern so
    the MCP network listener isn't wide open on the Tailscale interface.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from agent_policy import ConcurrencyGate

    gate = ConcurrencyGate()

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            client_ip = request.client.host if request.client else "unknown"
            auth_header = request.headers.get("Authorization", "")
            presented = auth_header[7:] if auth_header.startswith("Bearer ") else ""
            if presented and _constant_time_eq(presented, token):
                logger.info("MCP: authenticated request from %s (%s %s)", client_ip, request.method, request.url.path)
                return await call_next(request)
            policy = store.authenticate(presented) if (store is not None and presented) else None
            if policy is None:
                logger.warning("MCP: auth failed from %s (%s %s)", client_ip, request.method, request.url.path)
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            tool = _tool_call_name(await request.body())
            if not tool:
                return await call_next(request)
            if not policy.allows(tool):
                store.record_denial(policy.agent_id, "mcp", "forbidden_tool", tool)
                logger.warning("MCP: agent %s denied tool %s", policy.agent_id, tool)
                return JSONResponse({"error": "forbidden", "agent": policy.agent_id, "tool": tool}, status_code=403)
            wait = store.take_rate_slot(policy)
            if wait is not None:
                store.record_denial(policy.agent_id, "mcp", "rate_limited", tool)
                return _too_many(policy.agent_id, wait)
            if not gate.try_acquire(policy):
                store.record_denial(policy.agent_id, "mcp", "too_many_concurrent", tool)
                return _too_many(policy.agent_id, 1.0)
            try:
                return await call_next(request)
            finally:
                gate.release(policy)

    return BearerAuthMiddleware


def _too_many(agent_id: str, wait: float):
    from starlette.responses import JSONResponse

    seconds = str(int(wait + 0.999))
    return JSONResponse(
        {"error": "rate_limited", "agent": agent_id, "retry_after": int(seconds)},
        status_code=429, headers={"Retry-After": seconds},
    )


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _build_tool_function(tool_def: Dict[str, Any], task_id: str) -> Callable[..., str]:
    """Build a real Python function matching a tool's JSON schema.

    FastMCP infers each MCP tool's input schema from the wrapped function's
    signature (via pydantic, ``Annotated[type, Field(description=...)]`` for
    per-parameter descriptions) — not from an arbitrary JSON schema — so we
    generate a function whose signature mirrors the tool's declared
    parameters, then dispatch the call through ``handle_function_call``.
    """
    fn_schema = tool_def["function"]
    name = fn_schema["name"]
    description = fn_schema.get("description", "") or f"Hermes tool: {name}"
    params_schema = fn_schema.get("parameters") or {}
    properties: Dict[str, Any] = params_schema.get("properties") or {}
    required = set(params_schema.get("required") or [])

    # Required params first (no default), then optional (default None) —
    # Python signatures require non-default params before defaulted ones.
    ordered_names = sorted(properties.keys(), key=lambda n: (n not in required, n))

    arg_lines = []
    for pname in ordered_names:
        prop = properties.get(pname) or {}
        py_type = _JSON_TYPE_TO_PY.get(prop.get("type"), "str")
        pdesc = (prop.get("description") or "").replace("\n", " ").strip()
        # repr() safely escapes the (arbitrary, schema-provided) description
        # for embedding as a Python source literal.
        field_expr = f"Field(description={pdesc!r})"
        if pname in required:
            arg_lines.append(f"{pname}: Annotated[{py_type}, {field_expr}]")
        else:
            arg_lines.append(f"{pname}: Annotated[Optional[{py_type}], {field_expr}] = None")

    # Signature built via exec so each parameter gets a real name/type/
    # description (needed for FastMCP's pydantic-based schema inference);
    # the tool body just forwards everything to the real dispatcher.
    signature = ", ".join(arg_lines)
    func_source = f"def _tool({signature}) -> str:\n    return _dispatch(locals())\n"

    def _dispatch(call_args: Dict[str, Any]) -> str:
        from model_tools import handle_function_call
        clean_args = {k: v for k, v in call_args.items() if v is not None}
        try:
            return handle_function_call(
                function_name=name,
                function_args=clean_args,
                task_id=task_id,
            )
        except Exception as e:
            logger.exception("MCP external tool '%s' failed", name)
            return json.dumps({"error": str(e)})

    namespace: Dict[str, Any] = {
        "_dispatch": _dispatch,
        "Optional": Optional,
        "Annotated": Annotated,
        "Field": Field,
    }
    exec(compile(func_source, f"<mcp-external-tool:{name}>", "exec"), namespace)
    fn = namespace["_tool"]
    fn.__name__ = name
    fn.__doc__ = description
    return fn


def register_external_tools(mcp, toolset_name: str, task_id: str) -> List[str]:
    """Register every tool in *toolset_name* as an MCP tool on *mcp*.

    Returns the list of tool names actually registered (tools without a
    satisfied ``check_fn`` — e.g. a missing API key — are skipped, matching
    normal toolset behavior).
    """
    import model_tools  # noqa: F401 — import triggers discover_builtin_tools()
    from model_tools import get_tool_definitions

    definitions = get_tool_definitions(enabled_toolsets=[toolset_name])
    registered: List[str] = []
    for tool_def in definitions:
        fn = _build_tool_function(tool_def, task_id=task_id)
        mcp.add_tool(fn, name=fn.__name__, description=tool_def["function"].get("description", ""))
        registered.append(fn.__name__)

    logger.info(
        "MCP: exposed %d tool(s) from toolset '%s': %s",
        len(registered), toolset_name, ", ".join(registered),
    )
    return registered


def enable_dangerous_tool_approvals(bridge, session_key: str) -> Callable[[], None]:
    """Route dangerous-command approvals through the existing chat-bridge
    ``permissions_list_open`` / ``permissions_respond`` MCP tools.

    Sets the same env flags the TUI/gateway use so ``tools/approval.py``
    treats this process as an interactive session requiring approval
    (never the silent auto-approve fallback for non-CLI/non-gateway
    contexts), binds *session_key* as the active approval session, and
    registers a notify callback that feeds ``bridge``'s pending-approvals
    queue.

    Returns an ``unregister()`` callable to undo the wiring on shutdown.
    """
    from tools.approval import (
        register_gateway_notify,
        resolve_gateway_approval,
        set_current_session_key,
        unregister_gateway_notify,
    )

    os.environ["HERMES_GATEWAY_SESSION"] = "1"
    os.environ["HERMES_EXEC_ASK"] = "1"
    token = set_current_session_key(session_key)

    _DECISION_MAP = {"allow-once": "once", "allow-always": "always", "deny": "deny"}

    def _on_approval_requested(approval_data: dict) -> None:
        approval_id = uuid.uuid4().hex[:12]
        bridge.track_dangerous_approval(approval_id, session_key, approval_data)

    register_gateway_notify(session_key, _on_approval_requested)

    # Wire the bridge's existing respond_to_approval() to also resolve the
    # real approval.py queue (not just its own bookkeeping dict).
    bridge.set_dangerous_approval_resolver(
        lambda session_key, decision: resolve_gateway_approval(
            session_key, _DECISION_MAP.get(decision, "deny"),
        )
    )

    def _unregister() -> None:
        unregister_gateway_notify(session_key)
        from tools.approval import reset_current_session_key
        reset_current_session_key(token)

    return _unregister
