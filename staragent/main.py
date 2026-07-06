from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import socket
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from staragent.auth import (
    auth_token_path,
    create_stored_auth_token,
    hub_auth_token,
    node_auth_token,
    write_stored_auth_token,
)
from staragent.dependencies import ensure_dependencies
from staragent.hub import node_by_name, request_json
from staragent.presets import COMMAND_PRESETS, preset_command, preset_names
from staragent.runtime import (
    ensure_tmux_session,
    kill_tmux_session,
    start_tmux_worker,
    wait_for_tmux_session,
)
from staragent.schemas import CreateWorker
from staragent.status import collect_session_views

app = typer.Typer(help="Monitor and control AI coding-agent sessions.")
console = Console()


@app.command()
def presets() -> None:
    """List available coding CLI command presets."""
    table = Table(title="StarAgent Command Presets")
    table.add_column("Preset", style="bold")
    table.add_column("Command")
    table.add_column("Label")
    for preset in COMMAND_PRESETS:
        table.add_row(preset.name, preset.command, preset.label)
    console.print(table)


@app.command()
def create(
    name: str = typer.Argument(..., help="tmux session name to create."),
    cwd: Path = typer.Option(
        Path.cwd(),
        "--cwd",
        "-C",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Working directory for the session.",
    ),
    preset: str = typer.Option(
        "codex-yolo",
        "--preset",
        "-p",
        help=f"Command preset. Available: {preset_names()}",
    ),
    command: str = typer.Option(
        "",
        "--cmd",
        help="Custom command. Overrides --preset.",
    ),
    node_name: str = typer.Option(
        "local",
        "--node",
        "-n",
        help="Node to create the session on. Defaults to local.",
    ),
) -> None:
    """Create a tmux-backed coding-agent session."""
    command = command.strip() or resolve_preset_command(preset)
    worker = CreateWorker(name=name, cwd=str(cwd), command=command)
    try:
        node = node_by_name(node_name)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown node: {node_name}") from exc

    try:
        if node.is_local:
            start_tmux_worker(worker.name, worker.cwd, worker.command)
        else:
            request_json(node, "POST", "/api/workers", worker.model_dump())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (RuntimeError, OSError, urllib.error.URLError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(1) from exc

    console.print(f"Created session: {worker.name}", style="green")
    console.print(f"Node: {node.name}")
    console.print(f"Working directory: {worker.cwd}")
    console.print(f"Command: {worker.command}")
    console.print(f"Attach: tmux attach -t {shlex.quote(worker.name)}")


@app.command()
def ps() -> None:
    """List live local tmux sessions."""
    views = collect_session_views()
    table = Table(title="StarAgent Sessions")
    table.add_column("Name", style="bold")
    table.add_column("Node")
    table.add_column("Type")
    table.add_column("Agent")
    table.add_column("Status")
    table.add_column("Repo")
    table.add_column("Branch")
    table.add_column("Last Updated")
    table.add_column("Source")
    table.add_column("Attach")
    table.add_column("Task")

    for view in views:
        table.add_row(
            view.name,
            view.node_name,
            view.session_type,
            view.agent,
            view.status,
            view.repo_name,
            view.branch,
            relative_time(view.last_updated),
            view.status_report.source if view.status_report else "config",
            ssh_attach_command(view.name),
            view.task,
        )
    console.print(table)


@app.command()
def dashboard(
    bind: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the local web dashboard."""
    ensure_dependencies()
    ensure_hub_auth_for_bind(bind)
    console.print(f"StarAgent dashboard: http://{bind}:{port}")
    uvicorn.run(
        "staragent.dashboard.app:create_app", factory=True, host=bind, port=port, reload=reload
    )


@app.command()
def hub(
    bind: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
    session: str = typer.Option("staragent-hub", "--session"),
) -> None:
    """Run the StarAgent hub dashboard inside a supervised tmux session."""
    ensure_dependencies()
    ensure_hub_auth_for_bind(bind)
    if os.environ.get("STARAGENT_TMUX_CHILD") == "hub":
        dashboard(bind=bind, port=port, reload=False)
        return
    command = tmux_child_command(
        "hub", ["staragent", "hub", "--host", bind, "--port", str(port), "--session", session]
    )
    console.print(f"StarAgent hub: tmux session {session} -> http://{bind}:{port}")
    ensure_tmux_session(session, str(Path.cwd()), command)
    wait_for_tmux_session(session)
    raise typer.Exit(1)


def run_node(bind: str, port: int, reload: bool) -> None:
    ensure_dependencies()
    if not remote_node_token():
        console.print(
            "STARAGENT_NODE_TOKEN or STARAGENT_AUTH_TOKEN is required before starting a node.",
            style="red",
        )
        raise typer.Exit(1)
    console.print(f"StarAgent node: http://{bind}:{port}")
    uvicorn.run("staragent.node.app:create_app", factory=True, host=bind, port=port, reload=reload)


@app.command()
def node(
    bind: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8081, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start a StarAgent remote node API for this machine."""
    run_node(bind, port, reload)


@app.command()
def lark(
    app_id: str = typer.Option("", "--app-id", help="Lark app id."),
    app_secret: str = typer.Option("", "--app-secret", help="Lark app secret."),
    verification_token: str = typer.Option("", "--verification-token"),
    encrypt_key: str = typer.Option("", "--encrypt-key"),
    allowed_users: str = typer.Option(
        "",
        "--allowed-users",
        help="Comma-separated Lark user open_id/user_id/union_id allowlist.",
    ),
    allowed_chats: str = typer.Option(
        "",
        "--allowed-chats",
        help="Comma-separated Lark chat_id allowlist.",
    ),
    allow_all: bool = typer.Option(False, "--allow-all", help="Allow all Lark senders."),
    dashboard_url: str = typer.Option(
        "", "--dashboard-url", help="Public StarAgent dashboard URL."
    ),
) -> None:
    """Run the Lark command integration worker."""
    from staragent.integrations.lark import LarkConfig, run_lark_integration

    try:
        config = LarkConfig.from_env(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            encrypt_key=encrypt_key,
            allowed_users=allowed_users,
            allowed_chats=allowed_chats,
            allow_all=allow_all or None,
            dashboard_url=dashboard_url,
        )
    except ValueError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(1) from exc
    console.print("StarAgent Lark integration: WebSocket worker started")
    try:
        run_lark_integration(config)
    except RuntimeError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(1) from exc


@app.command()
def kill(name: str) -> None:
    """Stop a live tmux session."""
    try:
        kill_tmux_session(name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except RuntimeError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(1) from exc
    console.print(f"Stopped tmux session: {name}")


def resolve_preset_command(name: str) -> str:
    try:
        return preset_command(name)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown preset: {name}. Available: {preset_names()}") from exc


def relative_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    now = datetime.now(UTC).astimezone()
    delta = now - value.astimezone(now.tzinfo)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def ssh_attach_command(session: str) -> str:
    target = os.environ.get("STARAGENT_SSH_TARGET") or usable_ssh_target()
    remote = f"tmux attach -t {shlex.quote(session)}"
    return f"ssh -t {shlex.quote(target)} {shlex.quote(remote)}"


def tmux_child_command(kind: str, args: list[str]) -> str:
    executable = staragent_executable()
    env_parts = [
        f"STARAGENT_TMUX_CHILD={shlex.quote(kind)}",
        f"PATH={shlex.quote(os.environ.get('PATH', ''))}",
    ]
    for name in (
        "STARAGENT_STATE_DIR",
        "STARAGENT_NODES",
        "STARAGENT_NODE_TOKEN",
        "STARAGENT_SSH_TARGET",
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        value = os.environ.get(name)
        if value:
            env_parts.append(f"{name}={shlex.quote(value)}")
    return f"{' '.join(env_parts)} {shlex.quote(str(executable))} {shlex.join(args[1:])}"


def staragent_executable() -> Path:
    import sys

    found = shutil.which("staragent")
    if found:
        return Path(found).resolve()
    return Path(sys.executable).with_name("staragent")


def usable_ssh_target() -> str:
    fqdn = socket.getfqdn()
    if fqdn and not fqdn.startswith("localhost"):
        return fqdn
    return socket.gethostname()


def remote_node_token() -> str:
    return node_auth_token()


def ensure_hub_auth_for_bind(bind: str) -> None:
    env_token = os.environ.get("STARAGENT_AUTH_TOKEN", "").strip()
    if env_token:
        write_stored_auth_token(env_token)
        return
    if hub_auth_token() or is_loopback_bind(bind):
        return
    token = create_stored_auth_token()
    console.print(
        f"Generated STARAGENT_AUTH_TOKEN and saved it to {auth_token_path()}.",
        style="yellow",
    )
    console.print(
        f"Use this token to log in: {token}",
        style="yellow",
    )


def is_loopback_bind(bind: str) -> bool:
    value = (bind or "").strip().lower()
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    if value in {"0.0.0.0", "::", ""}:
        return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    app()
