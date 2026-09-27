"""Cross-agent tool result cache (tool_result_cache.py), driven through the real dispatcher.

Why: every agent repeated the same search on the same commit and paid for it again.
These tests run model_tools.handle_function_call on a real git repository in a
temp dir, so the key (repo, HEAD, clean tree) is exercised against real git, and
the cache lives in the per-test HERMES_HOME state.db (see tests/conftest.py).
@see tool_result_cache.py, model_tools.handle_function_call
"""
import json
import subprocess

import pytest

import tool_result_cache
from model_tools import handle_function_call

GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(repo, *args):
    subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def needle_function():\n    return 1\n")
    _git(root, "init", "-q")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _search(repo, task_id="agent-a"):
    return handle_function_call("search_files", {"pattern": "needle_function", "path": str(repo)}, task_id=task_id)


def test_second_identical_search_is_a_hit_across_tasks(repo):
    first = _search(repo, task_id="agent-a")
    second = _search(repo, task_id="agent-b")
    assert "needle_function" in first
    assert second == first
    assert tool_result_cache.stats()["search_files"] == {"hits": 1, "misses": 1, "bypasses": 0}


def test_new_commit_invalidates(repo):
    _search(repo)
    (repo / "b.py").write_text("needle_function()\n")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-q", "-m", "second")
    after = _search(repo)
    assert "b.py" in after
    assert tool_result_cache.stats()["search_files"]["misses"] == 2


def test_dirty_tree_bypasses_and_sees_uncommitted_change(repo):
    _search(repo)
    (repo / "c.py").write_text("needle_function()  # not committed\n")
    dirty = _search(repo)
    assert "c.py" in dirty
    assert tool_result_cache.stats()["search_files"] == {"hits": 0, "misses": 1, "bypasses": 1}


def test_outside_git_bypasses(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "x.txt").write_text("needle_function\n")
    handle_function_call("search_files", {"pattern": "needle_function", "path": str(plain)}, task_id="t")
    assert tool_result_cache.stats()["search_files"]["bypasses"] == 1


def test_read_file_is_never_cached(repo):
    for _ in range(2):
        handle_function_call("read_file", {"path": str(repo / "a.py")}, task_id="t")
    assert "read_file" not in tool_result_cache.stats()


def test_error_results_are_not_stored(repo):
    missing = repo / "does-not-exist"
    handle_function_call("search_files", {"pattern": "x", "path": str(missing)}, task_id="t")
    handle_function_call("search_files", {"pattern": "x", "path": str(missing)}, task_id="t")
    assert tool_result_cache.stats().get("search_files", {}).get("hits", 0) == 0


def test_cacheable_tools_are_a_subset_of_idempotent_tools():
    from agent.tool_guardrails import IDEMPOTENT_TOOL_NAMES

    assert set(tool_result_cache.CACHEABLE) <= IDEMPOTENT_TOOL_NAMES


def test_ttl_expiry(monkeypatch):
    key = tool_result_cache.CacheKey(tool="web_search", key="k", scope="ttl")
    tool_result_cache.store(key, json.dumps({"results": [1]}), now=1000.0)
    assert tool_result_cache.fetch(key, now=1000.0 + tool_result_cache.WEB_TTL_SECONDS - 1) is not None
    assert tool_result_cache.fetch(key, now=1000.0 + tool_result_cache.WEB_TTL_SECONDS + 1) is None
