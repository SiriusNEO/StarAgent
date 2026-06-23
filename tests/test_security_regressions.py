from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi.testclient import TestClient

from staragent import dependencies
from staragent.auth import auth_token_path, read_stored_auth_token, write_stored_auth_token
from staragent.dashboard import app as dashboard_app
from staragent.dashboard.app import (
    HTTP_TERMINAL_IDLE_SECONDS,
    HttpTerminal,
    cleanup_http_terminals,
    directory_listing,
    file_preview_payload,
    http_terminals,
    lark_connection_test_payload,
)
from staragent.dashboard.app import create_app as create_dashboard_app
from staragent.main import ensure_hub_auth_for_bind, is_loopback_bind, tmux_child_command
from staragent.node.app import create_app
from staragent.paths import PROJECT_ROOT, state_dir
from staragent.pty_terminal import (
    MAX_TERMINAL_INPUT_BYTES,
    TerminalOutputFilter,
    parse_client_message,
)


def test_parse_client_message_rejects_invalid_json() -> None:
    assert parse_client_message("not-json") == ("unknown", None)
    assert parse_client_message(json.dumps(["input"])) == ("unknown", None)


def test_parse_client_message_rejects_oversized_input() -> None:
    message = json.dumps({"type": "input", "data": "x" * (65 * 1024)})
    assert parse_client_message(message) == ("unknown", None)


def test_terminal_output_filter_keeps_scrollback_buffer() -> None:
    output_filter = TerminalOutputFilter()
    chunks = [
        b"history\r\n\x1b[?10",
        b"49h\x1b[22;0;0tlive\r\n\x1b[3J\x1bc",
    ]
    filtered = b"".join(output_filter.feed(chunk) for chunk in chunks) + output_filter.flush()
    assert filtered == b"history\r\nlive\r\n"


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


def test_node_api_requires_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
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


def test_hub_generates_token_for_non_loopback_bind(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STARAGENT_AUTH_TOKEN", raising=False)
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("localhost")
    assert not is_loopback_bind("0.0.0.0")

    ensure_hub_auth_for_bind("127.0.0.1")
    assert not auth_token_path().exists()

    ensure_hub_auth_for_bind("0.0.0.0")

    token = read_stored_auth_token()
    assert len(token) >= 32


def test_hub_persists_env_token_for_non_loopback_bind(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_AUTH_TOKEN", "secret")
    ensure_hub_auth_for_bind("0.0.0.0")
    assert read_stored_auth_token() == "secret"


def test_hub_tmux_child_reads_stored_auth_without_inlining_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_AUTH_TOKEN", "secret")
    monkeypatch.delenv("STARAGENT_NODE_TOKEN", raising=False)
    ensure_hub_auth_for_bind("0.0.0.0")

    command = tmux_child_command(
        "hub", ["staragent", "hub", "--host", "0.0.0.0", "--port", "8080"]
    )

    assert read_stored_auth_token() == "secret"
    assert "STARAGENT_AUTH_TOKEN=" not in command
    assert "secret" not in command


def test_state_dir_uses_project_state_dir_by_default(monkeypatch) -> None:
    monkeypatch.delenv("STARAGENT_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert state_dir() == PROJECT_ROOT / ".staragent"


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


def test_http_terminal_input_writes_to_terminal(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STARAGENT_AUTH_TOKEN", raising=False)

    class FakeTerminal:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, data: str) -> None:
            self.writes.append(data)

    terminal = FakeTerminal()
    row = HttpTerminal(
        terminal_id="live",
        node_name="local",
        session_name="demo",
        created_at=datetime.now().timestamp(),
        last_poll_at=datetime.now().timestamp(),
    )
    row.terminal = terminal  # type: ignore[assignment]
    http_terminals[row.terminal_id] = row
    client = TestClient(create_dashboard_app())
    try:
        response = client.post("/api/terminal-http/live/input", json={"data": "ls\r"})
        assert response.status_code == 200
        assert response.json() == {"status": "sent"}
        assert terminal.writes == ["ls\r"]

        oversized = client.post(
            "/api/terminal-http/live/input",
            json={"data": "x" * (MAX_TERMINAL_INPUT_BYTES + 1)},
        )
        assert oversized.status_code == 413
        assert terminal.writes == ["ls\r"]
    finally:
        http_terminals.pop(row.terminal_id, None)


def test_lark_connection_test_fails_fast_without_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    import staragent.dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "LARK_CONFIG_PATH", tmp_path / "lark_config.json")
    monkeypatch.delenv("STARAGENT_LARK_APP_ID", raising=False)
    monkeypatch.delenv("STARAGENT_LARK_APP_SECRET", raising=False)

    payload = lark_connection_test_payload()

    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["steps"][0]["name"] == "Configuration"
    assert "App ID" in payload["steps"][0]["detail"]


def test_lark_sdk_check_uses_worker_python(monkeypatch, tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    staragent = bin_dir / "staragent"
    python = bin_dir / "python"
    staragent.write_text("#!/bin/sh\n", encoding="utf-8")
    python.write_text("#!/bin/sh\n", encoding="utf-8")

    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        return Result()

    dashboard_app.LARK_SDK_CHECK_CACHE.clear()
    monkeypatch.setattr(dashboard_app.subprocess, "run", fake_run)

    assert dashboard_app.lark_sdk_installed(staragent)
    assert calls[0][0] == str(python)


def test_lark_page_shows_running_worker_readiness(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_LARK_APP_ID", "cli_test")
    monkeypatch.setenv("STARAGENT_LARK_APP_SECRET", "secret")
    monkeypatch.setenv("STARAGENT_LARK_ALLOW_ALL", "1")
    monkeypatch.delenv("STARAGENT_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(dashboard_app, "LARK_CONFIG_PATH", tmp_path / "lark_config.json")
    monkeypatch.setattr(dashboard_app, "tmux_session_exists", lambda name: True)
    monkeypatch.setattr(dashboard_app, "capture_tmux_pane_ansi", lambda name, lines=80: "")
    monkeypatch.setattr(dashboard_app, "lark_sdk_installed", lambda executable=None: True)

    response = TestClient(create_dashboard_app()).get("/lark")

    assert response.status_code == 200
    assert "Lark worker is running." in response.text


def test_lark_worker_uses_state_auth_token_without_inlining_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STARAGENT_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("STARAGENT_NODE_TOKEN", raising=False)
    monkeypatch.setenv("STARAGENT_LARK_APP_ID", "cli_test")
    monkeypatch.setenv("STARAGENT_LARK_APP_SECRET", "secret")
    monkeypatch.setenv("STARAGENT_LARK_ALLOW_ALL", "1")
    monkeypatch.setattr(dashboard_app, "LARK_CONFIG_PATH", tmp_path / "lark_config.json")
    monkeypatch.setattr(dashboard_app, "tmux_session_exists", lambda name: False)
    monkeypatch.setattr(dashboard_app, "lark_sdk_installed", lambda executable=None: True)
    write_stored_auth_token("stored-secret")

    payload = dashboard_app.lark_status_payload()
    auth_item = next(
        item for item in payload["config"]["items"] if item["name"] == "STARAGENT_AUTH_TOKEN"
    )
    worker_command = dashboard_app.lark_worker_command()

    assert auth_item["present"] is True
    assert auth_item["source"] == "state"
    assert f"STARAGENT_STATE_DIR={tmp_path}" in worker_command
    assert "STARAGENT_AUTH_TOKEN=" not in worker_command
    assert "stored-secret" not in worker_command


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
