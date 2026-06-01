from __future__ import annotations

import asyncio
import json
from datetime import datetime

import typer
from fastapi.testclient import TestClient

from staragent import dependencies
from staragent.dashboard.app import (
    HTTP_TERMINAL_IDLE_SECONDS,
    HttpTerminal,
    cleanup_http_terminals,
    directory_listing,
    file_preview_payload,
    http_terminals,
)
from staragent.main import ensure_hub_auth_for_bind, is_loopback_bind
from staragent.node.app import create_app
from staragent.paths import state_dir
from staragent.pty_terminal import parse_client_message


def test_parse_client_message_rejects_invalid_json() -> None:
    assert parse_client_message("not-json") == ("unknown", None)
    assert parse_client_message(json.dumps(["input"])) == ("unknown", None)


def test_parse_client_message_rejects_oversized_input() -> None:
    message = json.dumps({"type": "input", "data": "x" * (65 * 1024)})
    assert parse_client_message(message) == ("unknown", None)


def test_file_preview_is_limited_to_workspace_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed = workspace / "main.py"
    allowed.write_text("print('ok')\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")

    assert file_preview_payload(str(allowed), root=str(workspace))["text"] == "print('ok')\n"

    try:
        file_preview_payload(str(outside), root=str(workspace))
    except ValueError as exc:
        assert "outside workspace" in str(exc)
    else:
        raise AssertionError("outside file preview should fail")


def test_sensitive_paths_are_hidden_and_blocked(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ssh_dir = workspace / ".ssh"
    ssh_dir.mkdir()
    secret = ssh_dir / "id_ed25519"
    secret.write_text("private", encoding="utf-8")

    listing = directory_listing(str(workspace), include_files=True, root=str(workspace))
    assert ".ssh" not in {entry["name"] for entry in listing["entries"]}

    try:
        file_preview_payload(str(secret), root=str(workspace))
    except ValueError as exc:
        assert "sensitive path" in str(exc)
    else:
        raise AssertionError("sensitive file preview should fail")


def test_node_api_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("STARAGENT_NODE_TOKEN", raising=False)
    monkeypatch.delenv("STARAGENT_AUTH_TOKEN", raising=False)
    client = TestClient(create_app())

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sessions").status_code == 503


def test_node_api_accepts_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    client = TestClient(create_app())

    assert client.get("/api/sessions").status_code == 401
    response = client.get("/api/sessions", headers={"Authorization": "Bearer node-secret"})
    assert response.status_code == 200


def test_hub_requires_token_for_non_loopback_bind(monkeypatch) -> None:
    monkeypatch.delenv("STARAGENT_AUTH_TOKEN", raising=False)
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("localhost")
    assert not is_loopback_bind("0.0.0.0")

    ensure_hub_auth_for_bind("127.0.0.1")
    try:
        ensure_hub_auth_for_bind("0.0.0.0")
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("non-loopback hub bind without token should exit")


def test_hub_allows_non_loopback_bind_with_token(monkeypatch) -> None:
    monkeypatch.setenv("STARAGENT_AUTH_TOKEN", "secret")
    ensure_hub_auth_for_bind("0.0.0.0")


def test_state_dir_uses_user_state_dir_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STARAGENT_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert state_dir() == tmp_path / ".local" / "state" / "staragent"


def test_state_dir_honors_override(monkeypatch, tmp_path) -> None:
    override = tmp_path / "state"
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(override))
    assert state_dir() == override


def test_cleanup_http_terminals_closes_stale_terminal() -> None:
    class FakeTerminal:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    terminal = FakeTerminal()
    row = HttpTerminal(
        terminal_id="stale",
        node_name="local",
        session_name="demo",
        created_at=datetime.now().timestamp(),
        last_poll_at=datetime.now().timestamp() - HTTP_TERMINAL_IDLE_SECONDS - 1,
    )
    row.terminal = terminal  # type: ignore[assignment]
    http_terminals[row.terminal_id] = row
    try:
        asyncio.run(cleanup_http_terminals())
        assert "stale" not in http_terminals
        assert terminal.closed
    finally:
        http_terminals.pop(row.terminal_id, None)


def test_dependencies_report_tailscale_as_optional(monkeypatch) -> None:
    def fake_which(command: str) -> str | None:
        return f"/usr/bin/{command}" if command == "tmux" else None

    monkeypatch.setattr(dependencies.shutil, "which", fake_which)
    monkeypatch.setattr(dependencies, "dependency_version", lambda command: f"{command} version")

    rows = dependencies.dependencies_status()["dependencies"]
    by_name = {row["name"]: row for row in rows}

    assert by_name["tmux"]["required"] is True
    assert by_name["tmux"]["installed"] is True
    assert by_name["tailscale"]["required"] is False
    assert by_name["tailscale"]["installed"] is False


def test_ensure_dependencies_does_not_install_optional_items(monkeypatch) -> None:
    optional = dependencies.Dependency(
        "tailscale",
        "Tailscale",
        "tailscale",
        "",
        required=False,
    )
    monkeypatch.setattr(dependencies, "DEPENDENCIES", (optional,))
    monkeypatch.setattr(dependencies.shutil, "which", lambda command: None)

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("optional dependencies should not be installed automatically")

    monkeypatch.setattr(dependencies.subprocess, "run", fail_run)

    rows = dependencies.ensure_dependencies()["dependencies"]
    assert rows == [
        {
            "name": "tailscale",
            "label": "Tailscale",
            "required": False,
            "installed": False,
            "version": "",
            "install_command": "see tailscale/README.md",
            "note": "",
            "error": "",
            "changed": False,
            "ok": True,
            "log": "",
        }
    ]
