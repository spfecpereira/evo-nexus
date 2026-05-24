#!/usr/bin/env python3
"""Install EvoNexus systemd units for a single-user LAN deployment.

This mode runs services as the operator's normal Linux user instead of a
dedicated ``evonexus`` account, so npm/uv/Claude CLI paths stay consistent
with the interactive shell used during setup.
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo/root: sudo python3 scripts/install_lan_systemd.py")


def _resolve_user(user: str | None) -> pwd.struct_passwd:
    if user:
        return pwd.getpwnam(user)
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return pwd.getpwnam(sudo_user)
    return pwd.getpwnam(Path(WORKSPACE).owner())


def _unit_env(home: Path) -> str:
    path = (
        f"{home}/.npm-global/bin:"
        f"{home}/.local/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )
    return f"Environment=HOME={home}\nEnvironment=PATH={path}\n"


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)


def _cleanup_legacy_processes() -> None:
    """Stop old foreground/script-launched processes before systemd owns them."""
    patterns = [
        "terminal-server/bin/server.js",
        f"{WORKSPACE}/dashboard/backend/app.py",
        f"{WORKSPACE}/scheduler.py",
        "dashboard/backend.*app.py",
        "python.*scheduler.py",
    ]
    for pattern in patterns:
        _run(["pkill", "-f", pattern], check=False)

    for port in ("8080", "32352"):
        if shutil.which("fuser"):
            _run(["fuser", "-k", "-n", "tcp", port], check=False)
        if shutil.which("lsof"):
            result = _run(["lsof", "-ti", f"tcp:{port}"], check=False)
            for pid in result.stdout.splitlines():
                if pid.strip():
                    _run(["kill", "-TERM", pid.strip()], check=False)

    pid_file = WORKSPACE / "ADWs" / "logs" / "scheduler.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def _chown_workspace(user_info: pwd.struct_passwd) -> None:
    """Make runtime files created by previous root/evonexus runs writable."""
    uid = user_info.pw_uid
    gid = user_info.pw_gid
    skip_dirs = {".git", ".venv", ".uv-cache", "node_modules", "dist", "__pycache__"}
    for path in WORKSPACE.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            os.chown(path, uid, gid)
        except (FileNotFoundError, PermissionError):
            pass
    try:
        os.chown(WORKSPACE, uid, gid)
    except (FileNotFoundError, PermissionError):
        pass


def install_units(user_info: pwd.struct_passwd) -> None:
    user = user_info.pw_name
    group = user
    home = Path(user_info.pw_dir)
    logs = WORKSPACE / "logs"
    logs.mkdir(exist_ok=True)

    units_dir = Path("/etc/systemd/system")
    env = _unit_env(home)

    terminal_unit = f"""[Unit]
Description=EvoNexus Terminal Server
After=network.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={WORKSPACE}
{env}ExecStart=/usr/bin/env node dashboard/terminal-server/bin/server.js
Restart=always
RestartSec=3
StandardOutput=append:{logs}/terminal-server.log
StandardError=append:{logs}/terminal-server.log

[Install]
WantedBy=evo-nexus.service
"""

    dashboard_unit = f"""[Unit]
Description=EvoNexus Dashboard
After=network.target evo-nexus-terminal.service
Wants=evo-nexus-terminal.service

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={WORKSPACE}/dashboard/backend
{env}Environment=UV_CACHE_DIR={WORKSPACE}/.uv-cache
ExecStart={WORKSPACE}/.venv/bin/python app.py
Restart=always
RestartSec=3
StandardOutput=append:{logs}/dashboard.log
StandardError=append:{logs}/dashboard.log

[Install]
WantedBy=evo-nexus.service
"""

    scheduler_unit = f"""[Unit]
Description=EvoNexus Scheduler
After=network.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={WORKSPACE}
{env}Environment=UV_CACHE_DIR={WORKSPACE}/.uv-cache
ExecStart={WORKSPACE}/.venv/bin/python scheduler.py
Restart=always
RestartSec=3
StandardOutput=append:{logs}/scheduler.log
StandardError=append:{logs}/scheduler.log

[Install]
WantedBy=evo-nexus.service
"""

    target_unit = """[Unit]
Description=EvoNexus application stack
Wants=evo-nexus-dashboard.service evo-nexus-terminal.service evo-nexus-scheduler.service
After=evo-nexus-dashboard.service evo-nexus-terminal.service evo-nexus-scheduler.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
ExecStop=/bin/systemctl stop evo-nexus-dashboard.service evo-nexus-terminal.service evo-nexus-scheduler.service

[Install]
WantedBy=multi-user.target
"""

    _write(units_dir / "evo-nexus-terminal.service", terminal_unit)
    _write(units_dir / "evo-nexus-dashboard.service", dashboard_unit)
    _write(units_dir / "evo-nexus-scheduler.service", scheduler_unit)
    _write(units_dir / "evo-nexus.service", target_unit)

    _cleanup_legacy_processes()
    _chown_workspace(user_info)

    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", "evo-nexus.service"])
    _run(["systemctl", "restart", "evo-nexus.service"])


def purge_old_evonexus_user() -> None:
    for unit in [
        "evo-nexus.service",
        "evo-nexus-dashboard.service",
        "evo-nexus-terminal.service",
        "evo-nexus-scheduler.service",
    ]:
        _run(["systemctl", "stop", unit], check=False)

    old_home = Path("/home/evonexus")
    try:
        pwd.getpwnam("evonexus")
    except KeyError:
        if old_home.exists():
            shutil.rmtree(old_home)
        return

    _run(["pkill", "-u", "evonexus"], check=False)
    _run(["userdel", "-r", "evonexus"], check=False)
    if old_home.exists():
        shutil.rmtree(old_home)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", help="Linux user that should run EvoNexus")
    parser.add_argument(
        "--purge-evonexus",
        action="store_true",
        help="Remove the old dedicated evonexus user and its home directory",
    )
    args = parser.parse_args()

    _require_root()
    user_info = _resolve_user(args.user)

    if args.purge_evonexus:
        purge_old_evonexus_user()

    install_units(user_info)
    print(f"Installed EvoNexus systemd services for user {user_info.pw_name}.")
    print("Check with: systemctl status evo-nexus.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
