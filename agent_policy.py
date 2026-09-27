"""Per-agent authorization, rate limit and concurrency for the Hermes network surfaces.

WHY: until F2 the MCP (8643) and API (8642) listeners accepted one shared token, so
every caller on the tailnet was "the operator": no way to tell agents apart, cap one
that loops, or forbid a tool to a reviewer. Each agent now has its own token, issued
once and stored only as sha256, bound to an ``agent_id`` (the agent's agentbus name,
the CN of its mTLS certificate).

ROLE IN THE FLOW: both surfaces call the same store. The MCP bearer middleware
(mcp_tools_bridge.build_bearer_auth_middleware) checks ``tools/call`` against
``allows()`` and the quota; the API server middleware
(gateway/platforms/api_server.APIServerAdapter.agent_policy_middleware) applies the
quota and concurrency to every authenticated request.

CONTRACT:
- ``authenticate(token)`` -> AgentPolicy, or None for an unknown/disabled token.
- ``take_rate_slot(policy)`` -> None when allowed, else seconds until the window
  resets (the Retry-After value). Every attempt counts, allowed or not.
- ``ConcurrencyGate`` caps simultaneous requests per agent.
- ``record_denial`` persists refusals so the portal shows them, not only the log.
The legacy shared tokens (MCP_SERVER_TOKEN / API_SERVER_KEY) stay valid as the
unrestricted "operator"; the callers handle that before reaching this module.

GOTCHA: the rate window lives in state.db, so one quota covers both surfaces (they
are separate processes). The concurrency gate is in memory, so its cap is per
surface. Tables are declared in hermes_state.SCHEMA_SQL (single schema owner),
created here by opening SessionDB once.
@see hermes_state.py (agent_policies, agent_rate_windows, agent_denials)
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WINDOW_SECONDS = 60
RATE_WINDOW_RETENTION_SECONDS = 3600


@dataclass(frozen=True)
class AgentPolicy:
    agent_id: str
    tools: tuple[str, ...]
    rate_per_min: int
    max_concurrent: int

    def allows(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in self.tools)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PolicyStore:
    def __init__(self, db_path: Optional[Path] = None):
        from hermes_state import DEFAULT_DB_PATH, SessionDB

        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        SessionDB(self.db_path)  # ensures the policy tables exist (declarative schema)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def add_agent(self, agent_id: str, tools: list[str], rate_per_min: int = 60, max_concurrent: int = 4) -> str:
        """Create (or rotate) an agent's token. Returns the plaintext token, shown only here."""
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_policies (agent_id, token_sha256, tools, rate_per_min, max_concurrent, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT(agent_id) DO UPDATE SET token_sha256=excluded.token_sha256, "
                "tools=excluded.tools, rate_per_min=excluded.rate_per_min, max_concurrent=excluded.max_concurrent, enabled=1",
                (agent_id, _hash(token), json.dumps(tools), rate_per_min, max_concurrent, time.time()),
            )
        return token

    def set_enabled(self, agent_id: str, enabled: bool) -> None:
        with self._connect() as conn:
            if conn.execute("UPDATE agent_policies SET enabled=? WHERE agent_id=?", (int(enabled), agent_id)).rowcount != 1:
                raise KeyError(f"agent_policies: no agent {agent_id!r}")

    def authenticate(self, token: str) -> Optional[AgentPolicy]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT agent_id, tools, rate_per_min, max_concurrent FROM agent_policies WHERE token_sha256=? AND enabled=1",
                (_hash(token),),
            ).fetchone()
        if row is None:
            return None
        return AgentPolicy(row["agent_id"], tuple(json.loads(row["tools"])), row["rate_per_min"], row["max_concurrent"])

    def take_rate_slot(self, policy: AgentPolicy, now: Optional[float] = None) -> Optional[float]:
        now = time.time() if now is None else now
        window = int(now // WINDOW_SECONDS) * WINDOW_SECONDS
        with self._connect() as conn:
            (count,) = conn.execute(
                "INSERT INTO agent_rate_windows (agent_id, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT(agent_id, window_start) DO UPDATE SET count=count+1 RETURNING count",
                (policy.agent_id, window),
            ).fetchone()
            conn.execute("DELETE FROM agent_rate_windows WHERE window_start < ?", (window - RATE_WINDOW_RETENTION_SECONDS,))
        if count <= policy.rate_per_min:
            return None
        return max(1.0, window + WINDOW_SECONDS - now)

    def record_denial(self, agent_id: str, surface: str, reason: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_denials (ts, agent_id, surface, reason, detail) VALUES (?, ?, ?, ?, ?)",
                (time.time(), agent_id, surface, reason, detail),
            )

    def recent_denials(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, agent_id, surface, reason, detail FROM agent_denials ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def list_agents(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_id, tools, rate_per_min, max_concurrent, enabled FROM agent_policies ORDER BY agent_id"
            ).fetchall()
        return [dict(r) for r in rows]


class ConcurrencyGate:
    """In-process cap of simultaneous requests per agent (one gate per surface process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = {}

    def try_acquire(self, policy: AgentPolicy) -> bool:
        with self._lock:
            current = self._inflight.get(policy.agent_id, 0)
            if current >= policy.max_concurrent:
                return False
            self._inflight[policy.agent_id] = current + 1
            return True

    def release(self, policy: AgentPolicy) -> None:
        with self._lock:
            self._inflight[policy.agent_id] = max(0, self._inflight.get(policy.agent_id, 0) - 1)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m agent_policy", description="Manage per-agent tokens and quotas.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="create or rotate an agent token (printed once)")
    add.add_argument("agent_id")
    add.add_argument("--tools", default="*", help="comma-separated fnmatch patterns of allowed MCP tools")
    add.add_argument("--rate", type=int, default=60, help="calls per minute")
    add.add_argument("--concurrent", type=int, default=4)
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("agent_id")
    sub.add_parser("list")
    sub.add_parser("denials")
    args = parser.parse_args(argv)

    store = PolicyStore()
    if args.cmd == "add":
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
        print(store.add_agent(args.agent_id, tools, args.rate, args.concurrent))
    elif args.cmd in ("disable", "enable"):
        store.set_enabled(args.agent_id, args.cmd == "enable")
    elif args.cmd == "list":
        print(json.dumps(store.list_agents(), indent=1))
    else:
        print(json.dumps(store.recent_denials(), indent=1))


if __name__ == "__main__":
    main()
