from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from staragent import dependencies
from staragent.auth import auth_token_path, read_stored_auth_token, write_stored_auth_token
from staragent.dashboard import app as dashboard_app
from staragent.dashboard.app import (
    HTTP_TERMINAL_IDLE_SECONDS,
    HttpTerminal,
    cleanup_http_terminals,
    http_terminals,
    lark_connection_test_payload,
)
from staragent.dashboard.app import create_app as create_dashboard_app
from staragent.files import (
    directory_listing,
    file_preview_payload,
    file_raw_info_payload,
    file_raw_payload,
)
from staragent.hub import NodeEntry
from staragent.main import (
    ensure_hub_auth_for_bind,
    is_loopback_bind,
    start_node_tmux_session,
    tmux_child_command,
)
from staragent.node.app import create_app
from staragent.paths import PROJECT_ROOT, state_dir
from staragent.pty_terminal import (
    MAX_TERMINAL_INPUT_BYTES,
    TerminalOutputFilter,
    parse_client_message,
)
from staragent.transcript import TranscriptMessage


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


def test_terminal_output_filter_emits_regular_output_immediately() -> None:
    output_filter = TerminalOutputFilter()

    assert output_filter.feed(b"x") == b"x"
    assert output_filter.feed(b"short prompt") == b"short prompt"
    assert output_filter.flush() == b""


def test_terminal_output_filter_only_buffers_partial_control_sequences() -> None:
    output_filter = TerminalOutputFilter()

    assert output_filter.feed(b"before\x1b[?10") == b"before"
    assert output_filter.feed(b"49hafter") == b"after"
    assert output_filter.feed(b"\x1b[") == b""
    assert output_filter.feed(b"31mred") == b"\x1b[31mred"


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


def test_theme_background_processing_creates_thumbnail(tmp_path, monkeypatch) -> None:
    from PIL import Image

    monkeypatch.setattr(dashboard_app, "THEME_BACKGROUND_DIR", tmp_path / "theme")
    monkeypatch.setattr(
        dashboard_app,
        "THEME_BACKGROUND_CONFIG_PATH",
        tmp_path / "theme" / "theme.json",
    )
    monkeypatch.setattr(
        dashboard_app,
        "THEME_BACKGROUND_LIBRARY_DIR",
        tmp_path / "theme" / "backgrounds",
    )

    image = Image.new("RGB", (1800, 1000), (102, 8, 116))
    data = io.BytesIO()
    image.save(data, format="PNG")

    dashboard_app.write_optimized_theme_background("abcdefabcdef", data.getvalue())
    entries = dashboard_app.theme_background_entries()

    assert len(entries) == 1
    assert entries[0]["path"].name == "abcdefabcdef.webp"
    assert entries[0]["thumb_path"].name == "abcdefabcdef.thumb.webp"
    assert entries[0]["thumb_url"] == "/api/theme/backgrounds/abcdefabcdef/thumb"
    assert entries[0]["thumb_path"].stat().st_size < entries[0]["path"].stat().st_size


def test_file_raw_assets_are_limited_to_safe_workspace_files(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    pdf = workspace / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    text = workspace / "README.md"
    text.write_text("# no raw access\n", encoding="utf-8")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    secret_dir = workspace / ".ssh"
    secret_dir.mkdir()
    secret = secret_dir / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n")

    body, media_type = file_raw_payload(str(image), root=str(workspace))
    assert body == b"\x89PNG\r\n\x1a\n"
    assert media_type == "image/png"
    body, media_type = file_raw_payload(str(pdf), root=str(workspace))
    assert body == b"%PDF-1.7\n"
    assert media_type == "application/pdf"
    assert file_raw_info_payload(str(pdf), root=str(workspace)) == {
        "path": str(pdf),
        "name": "paper.pdf",
        "size": len(b"%PDF-1.7\n"),
        "media_type": "application/pdf",
    }

    for path, expected in [
        (text, "only supported for images and PDFs"),
        (outside, "outside workspace"),
        (secret, "sensitive path"),
    ]:
        try:
            file_raw_payload(str(path), root=str(workspace))
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"raw file access should fail for {path}")


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


def test_versioned_static_assets_are_immutable() -> None:
    client = TestClient(create_dashboard_app())

    versioned = client.get("/static/styles.css?v=123")
    unversioned = client.get("/static/styles.css")

    assert versioned.status_code == 200
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert unversioned.status_code == 200
    assert unversioned.headers["cache-control"] == "public, max-age=86400"


def test_session_heavy_assets_are_loaded_on_demand() -> None:
    template = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "session.html").read_text(
        encoding="utf-8"
    )
    script = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "session.js").read_text(
        encoding="utf-8"
    )
    head = template.split("{% block head_extra %}", 1)[1].split("{% endblock %}", 1)[0]

    assert "xterm.min.js" not in head
    assert "highlight.min.js" not in head
    assert "const ensureTerminalAssets" in script
    assert "const ensureHighlightAssets" in script
    assert "static_version('session.js')" in template


def test_session_detail_has_im_style_session_switcher() -> None:
    template = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "session.html").read_text(
        encoding="utf-8"
    )
    script = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "session.js").read_text(
        encoding="utf-8"
    )

    assert "session-detail-layout" in template
    assert "session-switcher-item" in template
    assert "session-switcher-state pill status-{{ item.status }}" in template
    assert ">{{ item.status }}</span>" in template
    assert "session-switcher-avatar" not in template
    assert 'aria-current="page"' in template
    assert "session-switcher-toggle" in script
    assert "Find a session" not in template
    assert "session-switcher-search" not in template
    assert "session-switcher-create" not in template
    assert "← Sessions" not in template


def test_page_root_contains_horizontal_overflow_without_breaking_inner_scrollers() -> None:
    styles = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert "overscroll-behavior-x: none" in styles
    assert "@supports (overflow: clip)" in styles
    assert "overflow-x: clip" in styles
    assert "@supports not (overflow: clip)" in styles
    assert ".session-detail-main > *" in styles
    assert ".table-wrap" in styles and "overflow-x: auto" in styles


def test_chat_removes_only_the_middle_message_list_frame() -> None:
    styles = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )
    log_rule = styles.split(".chat-log {", 1)[1].split("}", 1)[0]
    bubble_rule = styles.split(".chat-message pre {", 1)[1].split("}", 1)[0]

    assert "background: transparent" in log_rule
    assert "border: 0" in log_rule
    assert "border-radius: 0" in log_rule
    assert "border-radius: 8px" in bubble_rule
    assert ".chat-user pre" in styles
    assert ".chat-agent pre" in styles


def test_tailscale_dashboard_adds_direct_nodes() -> None:
    template = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "nodes.html").read_text(
        encoding="utf-8"
    )
    script = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "nodes.js").read_text(
        encoding="utf-8"
    )

    assert 'button.textContent = "Add Tailscale"' in script
    assert '{mode: "lan", name: peer.name || peer.preferred_node' in script
    assert '{mode: "remote", name: peer.name || peer.preferred_node' not in script
    assert "Direct · LAN / Tailscale" in template


def test_agents_page_checks_clis_without_blocking_initial_render() -> None:
    template = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "agents.html").read_text(
        encoding="utf-8"
    )
    script = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "agents.js").read_text(
        encoding="utf-8"
    )

    assert "Agent CLIs" in template
    assert "Codex usage is read through its local account status RPC" in template
    assert 'class="agent-cli-usage" hidden' in template
    assert "renderAgentUsage" in script
    assert "remaining_percent" in script
    assert 'copy.dataset.copy = usage.action' in script
    assert "/agent-tools" in script
    assert "window.StarAgentAfterPaint(() => loadAgentTools(false))" in script
    assert "/agent-history" in script
    assert "Scanning starts only when requested" in template
    assert "source history files" not in script
    assert 'class="agent-cli-card" data-agent="{{ tool.name }}"' in template
    assert 'class="agent-cli-node-row" data-node="{{ node.name }}"' in template
    assert "agent-tools-node" not in template
    assert "Promise.all(nodeNames.map" in script


def test_session_creation_stays_on_sessions_page() -> None:
    agents = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "agents.html").read_text(
        encoding="utf-8"
    )
    sessions = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "Create Session" in sessions
    assert 'id="create-session"' in sessions
    assert "Create Worker" not in sessions
    assert "agent-launch-form" not in agents


def test_sessions_page_groups_sessions_by_node() -> None:
    template = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'class="sessions-node-groups"' in template
    assert 'class="sessions-node-group" data-node="{{ node.name }}"' in template
    assert "{% for node in node_views %}" in template
    assert "{% for view in node.sessions|sort(attribute='name') %}" in template
    assert 'node-status-{{ node.status }}' in template
    assert "<th>Node</th>" not in template


def test_page_javascript_is_served_as_versioned_static_assets() -> None:
    template_dir = PROJECT_ROOT / "staragent" / "dashboard" / "templates"
    static_dir = PROJECT_ROOT / "staragent" / "dashboard" / "static"

    for page in ("base", "index", "nodes", "agents", "logs", "lark", "session"):
        template = (template_dir / f"{page}.html").read_text(encoding="utf-8")
        script = static_dir / f"{page}.js"
        assert script.is_file()
        assert f"static_version('{page}.js')" in template


def test_sessions_page_defaults_worker_tools_to_local_node() -> None:
    template = (PROJECT_ROOT / "staragent" / "dashboard" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    script = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "index.js").read_text(
        encoding="utf-8"
    )

    assert 'node.name == "local"' in template
    assert 'cwdInput.value = ""' in script
    assert 'workerExplorer.load("")' in script


def test_remote_transcript_state_falls_back_to_terminal_output(monkeypatch) -> None:
    node = NodeEntry(name="old-node", url="http://old-node:8081", mode="lan")

    def fake_request_json(node, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        if "transcript-state" in path:
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO(b"Not Found"))
        assert path.endswith("/output?lines=500")
        return {"output": "legacy node answer"}

    monkeypatch.setattr(dashboard_app, "node_by_name", lambda name: node)
    monkeypatch.setattr(dashboard_app, "request_json", fake_request_json)
    monkeypatch.setattr(
        dashboard_app,
        "collect_node_view",
        lambda node, prefer_cached=False: SimpleNamespace(sessions=()),
    )

    state = dashboard_app.node_transcript_state("old-node", "dev")

    assert state.reply == "legacy node answer"
    assert state.final is True


def test_remote_request_errors_preserve_upstream_status() -> None:
    error = urllib.error.HTTPError(
        "http://node/api/directories",
        400,
        "Bad Request",
        {},
        io.BytesIO(b'{"detail":"Path does not exist"}'),
    )

    translated = dashboard_app.remote_request_exception(error)

    assert translated.status_code == 400
    assert translated.detail == "Path does not exist"


def test_session_chat_uses_one_transcript_poll_scheduler() -> None:
    script = (PROJECT_ROOT / "staragent" / "dashboard" / "static" / "session.js").read_text(
        encoding="utf-8"
    )

    assert "const scheduleTranscriptSync" in script
    assert "scheduleTranscriptSync(pollDelay)" in script
    assert "refreshChatOutput" not in script
    assert "workingTimer" not in script
    assert "setInterval(() => {\n    syncChatFromTranscript" not in script


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

    command = tmux_child_command("hub", ["staragent", "hub", "--host", "0.0.0.0", "--port", "8080"])

    assert read_stored_auth_token() == "secret"
    assert "STARAGENT_AUTH_TOKEN=" not in command
    assert "secret" not in command


def test_node_tmux_persists_auth_without_inlining_it(monkeypatch, tmp_path) -> None:
    commands: list[str] = []
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_AUTH_TOKEN", "secret")
    monkeypatch.delenv("STARAGENT_NODE_TOKEN", raising=False)
    monkeypatch.setattr("staragent.main.ensure_dependencies", lambda: None)
    monkeypatch.setattr("staragent.main.staragent_executable", lambda: "/bin/staragent")
    monkeypatch.setattr(
        "staragent.main.ensure_tmux_session",
        lambda _session, _cwd, command: commands.append(command),
    )

    start_node_tmux_session("127.0.0.1", 8081, "staragent-node")

    assert read_stored_auth_token() == "secret"
    assert commands
    assert "STARAGENT_AUTH_TOKEN=" not in commands[0]
    assert "secret" not in commands[0]


def test_node_tmux_persists_node_token_without_inlining_it(monkeypatch, tmp_path) -> None:
    commands: list[str] = []
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STARAGENT_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setattr("staragent.main.ensure_dependencies", lambda: None)
    monkeypatch.setattr("staragent.main.staragent_executable", lambda: "/bin/staragent")
    monkeypatch.setattr(
        "staragent.main.ensure_tmux_session",
        lambda _session, _cwd, command: commands.append(command),
    )

    start_node_tmux_session("127.0.0.1", 8081, "staragent-node")

    assert read_stored_auth_token() == "node-secret"
    assert commands
    assert "STARAGENT_NODE_TOKEN=" not in commands[0]
    assert "node-secret" not in commands[0]


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


def test_unchanged_transcript_history_is_not_rewritten(monkeypatch, tmp_path) -> None:
    history_path = tmp_path / "chat_history.json"
    monkeypatch.setattr(dashboard_app, "CHAT_HISTORY_PATH", history_path)
    writes = 0
    original_save = dashboard_app.save_chat_histories

    def tracking_save(data):  # type: ignore[no-untyped-def]
        nonlocal writes
        writes += 1
        original_save(data)

    monkeypatch.setattr(dashboard_app, "save_chat_histories", tracking_save)
    messages = (TranscriptMessage("user", "hello", 123, "message-1"),)

    first = dashboard_app.replace_chat_history_from_transcript("local", "dev", messages)
    second = dashboard_app.replace_chat_history_from_transcript("local", "dev", messages)

    assert first == second
    assert writes == 1


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
