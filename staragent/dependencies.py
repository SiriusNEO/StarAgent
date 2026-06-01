from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Dependency:
    name: str
    label: str
    command: str
    install_command: str
    required: bool = True
    note: str = ""


DEPENDENCIES = (
    Dependency("tmux", "tmux", "tmux", "", required=True, note="Required for all sessions."),
    Dependency(
        "tailscale",
        "Tailscale",
        "tailscale",
        "",
        required=False,
        note="Optional for remote nodes; LAN-only setups can skip it.",
    ),
)


def dependencies_status() -> dict[str, object]:
    return {"dependencies": [dependency_status(item) for item in DEPENDENCIES]}


def dependency_status(dependency: Dependency) -> dict[str, object]:
    executable = shutil.which(dependency.command)
    installed = bool(executable)
    return {
        "name": dependency.name,
        "label": dependency.label,
        "required": dependency.required,
        "installed": installed,
        "version": dependency_version(dependency.command) if installed else "",
        "install_command": install_command(dependency),
        "note": dependency.note,
        "error": "",
    }


def ensure_dependencies() -> dict[str, object]:
    results = []
    for dependency in DEPENDENCIES:
        before = dependency_status(dependency)
        if not dependency.required:
            results.append({**before, "changed": False, "ok": True, "log": ""})
            continue
        if before["installed"]:
            results.append({**before, "changed": False, "ok": True, "log": ""})
            continue
        command = install_command(dependency)
        try:
            result = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=240,
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                {
                    **before,
                    "changed": False,
                    "ok": False,
                    "error": f"install timed out: {exc}",
                    "log": "",
                }
            )
            continue
        after = dependency_status(dependency)
        ok = bool(after["installed"])
        log = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        results.append(
            {
                **after,
                "changed": ok,
                "ok": ok,
                "error": "" if ok else (result.stderr.strip() or result.stdout.strip()),
                "log": log[-4000:],
            }
        )
    return {"dependencies": results}


def install_command(dependency: Dependency) -> str:
    if dependency.name == "tmux":
        return tmux_install_command()
    if dependency.name == "tailscale":
        return tailscale_install_command()
    return run_as_root(dependency.install_command)


def tmux_install_command() -> str:
    managers = (
        (
            "apt-get",
            "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y tmux",
        ),
        ("dnf", "dnf install -y tmux"),
        ("yum", "yum install -y tmux"),
        ("apk", "apk add tmux"),
        ("pacman", "pacman -Sy --noconfirm tmux"),
        ("zypper", "zypper --non-interactive install tmux"),
        ("brew", "brew install tmux"),
    )
    for executable, command in managers:
        if shutil.which(executable):
            return run_as_root(command) if executable != "brew" else command
    return "install tmux with your system package manager"


def tailscale_install_command() -> str:
    managers = (
        ("apt-get", "curl -fsSL https://tailscale.com/install.sh | sh"),
        (
            "dnf",
            "dnf install -y 'dnf-command(config-manager)' && dnf config-manager --add-repo https://pkgs.tailscale.com/stable/fedora/tailscale.repo && dnf install -y tailscale",
        ),
        (
            "yum",
            "yum install -y yum-utils && yum-config-manager --add-repo https://pkgs.tailscale.com/stable/centos/8/tailscale.repo && yum install -y tailscale",
        ),
        ("apk", "apk add tailscale"),
        ("pacman", "pacman -Sy --noconfirm tailscale"),
        ("zypper", "zypper --non-interactive install tailscale"),
        ("brew", "brew install tailscale"),
    )
    for executable, command in managers:
        if shutil.which(executable):
            return run_as_root(command) if executable != "brew" else command
    return "see tailscale/README.md"


def run_as_root(command: str) -> str:
    if is_root():
        return command
    if shutil.which("sudo"):
        return f"sudo sh -lc {shell_quote(command)}"
    return command


def is_root() -> bool:
    try:
        import os

        return os.geteuid() == 0
    except AttributeError:
        return False


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def dependency_version(command: str) -> str:
    args = {
        "tmux": ["tmux", "-V"],
        "tailscale": ["tailscale", "version"],
    }.get(command, [command, "--version"])
    try:
        result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (
        (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else ""
    )
