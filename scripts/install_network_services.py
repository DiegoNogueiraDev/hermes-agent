#!/usr/bin/env python3
"""
Install the MCP (streamable-http) and API-server network listeners as
per-user systemd services, so they survive reboots without needing root.

Mirrors the unit template `hermes_cli/gateway.py::generate_systemd_unit()`
uses for `hermes gateway install`, but targets `hermes mcp serve
--transport streamable-http` and `hermes api-server` instead. Secrets
(API_SERVER_KEY, MCP_SERVER_TOKEN, API_SERVER_HOST) are never written into
the unit files — both commands call `load_hermes_dotenv()` at startup and
read them from ~/.hermes/.env directly, same as every other `hermes`
command.

Usage:
    python scripts/install_network_services.py [--uninstall]

No sudo required — installs to ~/.config/systemd/user/.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_cli.gateway import (  # noqa: E402
    PROJECT_ROOT,
    _build_user_local_paths,
    _detect_venv_dir,
    get_python_path,
)

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

# Explicit bind target for the MCP listener — passed as ExecStart args
# rather than relying on mcp_server.host/port in config.yaml, so the unit
# is correct regardless of whether that config section was ever set.
MCP_BIND_HOST = "100.70.169.61"  # this machine's Tailscale IP (lab-diego)
MCP_BIND_PORT = 8643
# The dashboard exposes API keys and has no robust auth (see
# hermes_cli/web_server.py::start_server), so it stays on loopback; reach it
# from another machine with `ssh -L 9119:127.0.0.1:9119 lab-diego`.
DASHBOARD_BIND_HOST = "127.0.0.1"
DASHBOARD_BIND_PORT = 9119


def _common_env(python_path: str, venv_dir: str, venv_bin: str) -> str:
    path_entries = [venv_bin]
    path_entries.extend(_build_user_local_paths(Path.home(), path_entries))
    path_entries.extend(["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"])
    sane_path = ":".join(path_entries)
    return (
        f'Environment="PATH={sane_path}"\n'
        f'Environment="VIRTUAL_ENV={venv_dir}"\n'
    )


def _unit(description: str, exec_start: str, working_dir: str, env_lines: str) -> str:
    return f"""[Unit]
Description={description}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={working_dir}
{env_lines}Restart=always
RestartSec=30
RestartMaxDelaySec=300
RestartSteps=5
KillMode=mixed
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def build_units() -> dict[str, str]:
    python_path = get_python_path()
    working_dir = str(PROJECT_ROOT)
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / "venv")
    venv_bin = str(detected_venv / "bin") if detected_venv else str(PROJECT_ROOT / "venv" / "bin")
    env_lines = _common_env(python_path, venv_dir, venv_bin)

    mcp_exec = (
        f"{python_path} -m hermes_cli.main mcp serve "
        f"--transport streamable-http --host {MCP_BIND_HOST} --port {MCP_BIND_PORT}"
    )
    # The gateway activates whichever platform adapters have credentials/
    # config enabling them; with only API_SERVER_ENABLED=true set (no
    # Telegram/Discord/etc tokens), it runs with just the API server active.
    api_exec = f"{python_path} -m hermes_cli.main gateway run --replace"
    dashboard_exec = (
        f"{python_path} -m hermes_cli.main dashboard "
        f"--host {DASHBOARD_BIND_HOST} --port {DASHBOARD_BIND_PORT} --no-open"
    )

    return {
        "hermes-mcp.service": _unit(
            "Hermes MCP server (streamable-http, Tailscale-reachable)",
            mcp_exec, working_dir, env_lines,
        ),
        "hermes-api-server.service": _unit(
            "Hermes OpenAI-compatible API server (Tailscale-reachable)",
            api_exec, working_dir, env_lines,
        ),
        "hermes-dashboard.service": _unit(
            "Hermes dashboard (loopback only; ssh -L 9119:127.0.0.1:9119)",
            dashboard_exec, working_dir, env_lines,
        ),
    }


def install() -> None:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    units = build_units()
    for name, content in units.items():
        path = UNIT_DIR / name
        path.write_text(content)
        print(f"Wrote {path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for name in units:
        subprocess.run(["systemctl", "--user", "enable", name], check=True)
        # restart (not "enable --now") so re-running this script after an
        # edit picks up unit changes even when the service is already active.
        subprocess.run(["systemctl", "--user", "restart", name], check=True)
        print(f"Enabled + (re)started {name}")

    print(
        "\nNote: for these to survive a reboot/logout even with nobody logged in, "
        "run once (needs sudo): loginctl enable-linger $USER"
    )


def uninstall() -> None:
    for name in build_units():
        subprocess.run(["systemctl", "--user", "disable", "--now", name], check=False)
        path = UNIT_DIR / name
        if path.exists():
            path.unlink()
            print(f"Removed {path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()
