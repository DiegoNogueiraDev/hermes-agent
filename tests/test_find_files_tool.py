"""find_files tool (tools/find_files_tool.py) against the real find-files service on the lab.

Why: the C4 ranker only reaches agents through this tool. These tests go through the real
dispatcher (model_tools.handle_function_call), a real git repo in a temp dir and the real
service at 127.0.0.1:8650, so they are marked ``integration`` (skipped by the default run,
see pyproject addopts). The unreachable-service case uses a real closed port, not a double.
@see tools/find_files_tool.py, tool_result_cache.py, ~/harness-universe/find-files/server.ts
"""
import json
import socket
import subprocess

import pytest

import tool_result_cache
from model_tools import handle_function_call

pytestmark = pytest.mark.integration
GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.ts").write_text("export function checkBearerToken(token: string) { return token.length > 0 }\n")
    (root / "src" / "render.ts").write_text("export function renderChart(data: number[]) { return data.map(String) }\n")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)
    return root


def _call(args):
    return json.loads(handle_function_call("find_files", args, task_id="t"))


def test_returns_ranked_files_of_the_repo(repo):
    out = _call({"task": "reject requests whose bearer token is empty", "path": str(repo), "top": 2})
    assert out["repo"] == str(repo.resolve())
    assert [f["path"] for f in out["files"]] == ["src/auth.ts", "src/render.ts"]
    assert len(out["commit"]) == 40


def test_second_identical_call_is_a_cache_hit(repo):
    args = {"task": "reject requests whose bearer token is empty", "path": str(repo), "top": 2}
    first, second = _call(dict(args)), _call(dict(args))
    assert first == second
    assert tool_result_cache.stats()["find_files"]["hits"] == 1


def test_path_outside_git_is_an_error_with_reason(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = _call({"task": "anything at all here", "path": str(plain)})
    assert "not inside a git repository" in out["error"]


def test_unreachable_service_is_an_error_with_reason(repo, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    monkeypatch.setenv("FIND_FILES_URL", f"http://127.0.0.1:{closed_port}/find")
    out = _call({"task": "reject requests whose bearer token is empty", "path": str(repo)})
    assert "find-files service unreachable" in out["error"]
