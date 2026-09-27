"""Cross-agent cache of read-only tool results, keyed by what the result depends on.

WHY: every agent on the lab (CLI, API runs, MCP callers, subagents) repeated the same
search on the same commit and the same web query, paying tokens and provider calls
again. The cache lives in state.db, so one agent's result serves the next one.

WHAT IS CACHED (subset of agent.tool_guardrails.IDEMPOTENT_TOOL_NAMES):
- ``search_files``: keyed by (git toplevel, HEAD). Only when the working tree is clean
  (``git status --porcelain`` empty, untracked included); a dirty tree or a path
  outside git is a counted *bypass*, so uncommitted edits are always seen.
- ``web_search`` / ``web_extract``: keyed by args only, valid for WEB_TTL_SECONDS.
NOT cached, on purpose: ``read_file`` (tools/file_tools._read_tracker records
read_timestamps that write_file/patch use to detect external changes; serving it from
here would skip that bookkeeping), ``session_search`` and ``browser_*`` (live state).
Results whose JSON carries an ``error`` are never stored.

CONTRACT: ``key_for`` -> CacheKey or None (None = not cacheable or bypassed);
``fetch`` counts hit/miss; ``store`` persists; ``stats`` returns per-tool counters.
A cache failure never breaks the tool call: it is logged and the call runs normally.
GOTCHA: files ignored by .gitignore do not show in ``git status``, so a search over
ignored files can be stale until the next commit.
@see model_tools.handle_function_call (the only caller), hermes_state.SCHEMA_SQL
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

WEB_TTL_SECONDS = 3600
GIT_ENTRY_MAX_AGE_SECONDS = 7 * 24 * 3600
GIT_TIMEOUT_SECONDS = 5
CACHEABLE = {"search_files": "git", "find_files": "git", "web_search": "ttl", "web_extract": "ttl"}

_initialized: set[str] = set()


@dataclass(frozen=True)
class CacheKey:
    tool: str
    key: str
    scope: str


def _db_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if str(path) not in _initialized:
        from hermes_state import SessionDB

        SessionDB(path)  # declarative schema creates tool_cache / tool_cache_stats
        _initialized.add(str(path))
    return sqlite3.connect(path, timeout=5)


def _bump(tool: str, column: str, saved_chars: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO tool_cache_stats (tool, {column}, saved_chars) VALUES (?, 1, ?) "
            f"ON CONFLICT(tool) DO UPDATE SET {column}={column}+1, saved_chars=saved_chars+excluded.saved_chars",
            (tool, saved_chars),
        )


def _git_scope(args: dict, task_id: str) -> Optional[str]:
    from tools.file_tools import _resolve_path_for_task

    target = _resolve_path_for_task(str(args.get("path") or "."), task_id)
    directory = target if target.is_dir() else target.parent
    if not directory.exists():
        return None

    def git(*cmd: str) -> Optional[str]:
        done = subprocess.run(["git", "-C", str(directory), *cmd], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS)
        return done.stdout if done.returncode == 0 else None

    head = git("rev-parse", "--show-toplevel", "HEAD")
    if not head:
        return None
    toplevel, sha = head.split()[:2]
    if git("status", "--porcelain") != "":
        return None
    return f"{toplevel}@{sha}"


def key_for(tool: str, args: dict, task_id: Optional[str]) -> Optional[CacheKey]:
    kind = CACHEABLE.get(tool)
    if kind is None:
        return None
    scope = _git_scope(args, task_id or "default") if kind == "git" else "ttl"
    if scope is None:
        _bump(tool, "bypasses")
        return None
    canonical = json.dumps({"tool": tool, "args": args, "scope": scope}, sort_keys=True, default=str)
    return CacheKey(tool, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), scope)


def fetch(key: CacheKey, now: Optional[float] = None) -> Optional[str]:
    now = time.time() if now is None else now
    with _connect() as conn:
        row = conn.execute("SELECT result FROM tool_cache WHERE key=? AND expires_at>?", (key.key, now)).fetchone()
    if row is None:
        _bump(key.tool, "misses")
        return None
    _bump(key.tool, "hits", len(row[0]))
    return row[0]


def _is_error(result: str) -> bool:
    try:
        parsed: Any = json.loads(result)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get("error"))


def store(key: CacheKey, result: str, now: Optional[float] = None) -> None:
    if not isinstance(result, str) or _is_error(result):
        return
    now = time.time() if now is None else now
    ttl = WEB_TTL_SECONDS if CACHEABLE.get(key.tool) == "ttl" else GIT_ENTRY_MAX_AGE_SECONDS
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tool_cache (key, tool, scope, result, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key.key, key.tool, key.scope, result, now, now + ttl),
        )
        conn.execute("DELETE FROM tool_cache WHERE expires_at <= ?", (now,))


def stats() -> dict[str, dict[str, int]]:
    with _connect() as conn:
        rows = conn.execute("SELECT tool, hits, misses, bypasses FROM tool_cache_stats").fetchall()
    return {tool: {"hits": h, "misses": m, "bypasses": b} for tool, h, m, b in rows}


def saved_chars() -> dict[str, int]:
    with _connect() as conn:
        return dict(conn.execute("SELECT tool, saved_chars FROM tool_cache_stats").fetchall())
