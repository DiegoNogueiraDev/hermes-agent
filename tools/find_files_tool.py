"""find_files: the files most likely to change for a task, ranked by the C4 service on the lab.

WHY: agents open files by guessing names or grepping; the C4 ranker (Rodada 7 of the Trilha
project) puts at least one right file in the top 5 for 65.6% of 90 real local tasks (Recall@5
38.7% against 22.4% for BM25), so asking it first saves reads. The ranker runs as a separate
service because it keeps two SweRankEmbed models loaded on the GPU; this tool only resolves the
repository and forwards the question.

CONTRACT: args {task (required), path (file or dir inside a git repo, default "."), top (1-50,
default 10)} -> JSON {repo, commit, files:[{path, score}], ms}. Paths are relative to the repo.
Any failure (not a git repo, service down, service 4xx/5xx) returns {"error": reason}; it never
returns an empty list in place of an error. Read-only, so tool_result_cache serves repeats on the
same commit (key: repo + HEAD, clean tree only).
GOTCHA: the service uses commit history as memory; the crm numbers were measured with PR history,
which changes the collab signal (5 of the top 20 in common), so treat crm-like repos with care.
@see ~/harness-universe/find-files/server.ts (service), tool_result_cache.py (cache)
"""
import json
import os
import subprocess
import urllib.error
import urllib.request

from tools.registry import registry

DEFAULT_URL = "http://127.0.0.1:8650/find"
TIMEOUT_SECONDS = 60

FIND_FILES_SCHEMA = {
    "name": "find_files",
    "description": (
        "Rank the files of a git repository most likely to need changes for a task, before reading "
        "or grepping. Returns paths relative to the repo with a score. Use it first when you do not "
        "know where a change belongs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The task in natural language (issue, bug report or request)."},
            "path": {"type": "string", "description": "Any file or directory inside the repository (default: current directory)."},
            "top": {"type": "integer", "description": "How many files to return, 1 to 50 (default 10)."},
        },
        "required": ["task"],
    },
}


def _error(reason: str) -> str:
    return json.dumps({"error": reason}, ensure_ascii=False)


def _repo_root(path: str, task_id: str) -> str | None:
    from tools.file_tools import _resolve_path_for_task

    target = _resolve_path_for_task(path or ".", task_id)
    directory = target if target.is_dir() else target.parent
    done = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, timeout=5)
    return done.stdout.strip() if done.returncode == 0 else None


def find_files_tool(task: str, path: str = ".", top: int = 10, task_id: str = "default") -> str:
    if not isinstance(task, str) or len(task.strip()) < 3:
        return _error("task: describe the task in natural language")
    repo = _repo_root(path, task_id)
    if repo is None:
        return _error(f"path {path!r} is not inside a git repository")
    url = os.environ.get("FIND_FILES_URL", DEFAULT_URL)
    body = json.dumps({"repo": repo, "task": task, "top": top}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return _error(f"find-files service answered {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return _error(f"find-files service unreachable at {url}: {exc}")
    return json.dumps({"repo": repo, **answer}, ensure_ascii=False)


def _handle_find_files(args, **kw):
    top = args.get("top", 10)
    if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= 50:
        return _error(f"top: integer from 1 to 50 (got {top!r})")
    return find_files_tool(task=args.get("task", ""), path=args.get("path", "."),
                           top=top, task_id=kw.get("task_id") or "default")


registry.register(name="find_files", toolset="file", schema=FIND_FILES_SCHEMA, handler=_handle_find_files,
                  emoji="🧭", max_result_size_chars=20_000)
