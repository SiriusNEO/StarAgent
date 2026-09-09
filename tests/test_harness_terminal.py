from __future__ import annotations

import asyncio
import contextlib

import pytest
from fastapi.testclient import TestClient

from staragent import harness_terminal, hub, pty_terminal
from staragent.dashboard import app as dashboard_app
from staragent.node import app as node_app


def test_harness_terminal_launches_selected_harness_before_the_shell(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeTerminal:
        closed = False

        def close(self) -> None:
            self.closed = True

    terminal = FakeTerminal()

    def fake_spawn(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((list(argv), kwargs))
        return terminal

    monkeypatch.setenv("STARAGENT_TEST_VALUE", "preserved")
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PROMPT_COMMAND", "missing_editor_hook")
    monkeypatch.setattr(harness_terminal, "login_shell", lambda: "/bin/bash")
    monkeypatch.setattr(
        harness_terminal.shutil,
        "which",
        lambda command: "/bin/bash" if command == "bash" else None,
    )
    monkeypatch.setattr(harness_terminal, "shell_working_directory", lambda: str(tmp_path))
    monkeypatch.setattr(harness_terminal.PtyTerminal, "spawn", fake_spawn)

    with harness_terminal.open_harness_terminal("codex") as opened:
        assert opened is terminal

    assert terminal.closed is True
    argv, kwargs = calls[0]
    assert argv[:2] == ["/bin/bash", "-lc"]
    assert "\ncodex\n" in argv[2]
    assert "Dropping into shell" in argv[2]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["STARAGENT_TEST_VALUE"] == "preserved"
    assert "PROMPT_COMMAND" not in kwargs["env"]
    assert kwargs["env"]["STARAGENT_HARNESS"] == "codex"
    assert kwargs["env"]["STARAGENT_HARNESS_COMMAND"] == "codex"
    assert kwargs["env"]["SHELL"] == "/bin/bash"
    assert kwargs["cols"] == 120
    assert kwargs["rows"] == 30


def test_harness_terminal_rejects_unknown_harness_before_spawning(monkeypatch) -> None:
    monkeypatch.setattr(
        harness_terminal.PtyTerminal,
        "spawn",
        lambda *_args, **_kwargs: pytest.fail("unknown Harness must not spawn a Shell"),
    )

    with (
        pytest.raises(ValueError, match="Unsupported Agent CLI"),
        harness_terminal.open_harness_terminal("unknown"),
    ):
        pass


def test_login_shell_prefers_the_service_environment(monkeypatch) -> None:
    monkeypatch.setenv("SHELL", "/custom/fish")
    monkeypatch.setattr(
        harness_terminal.shutil,
        "which",
        lambda value: value if value == "/custom/fish" else None,
    )

    assert harness_terminal.login_shell() == "/custom/fish"


def test_harness_auth_terminal_uses_only_allowlisted_flows(monkeypatch) -> None:
    monkeypatch.setattr(
        harness_terminal,
        "harness_process_environment",
        lambda _agent: {"PATH": "/tools"},
    )
    monkeypatch.setattr(
        harness_terminal.shutil,
        "which",
        lambda command, **_kwargs: f"/tools/{command}",
    )

    assert harness_terminal.harness_auth_argv("codex", "login", "browser") == [
        "/tools/codex",
        "login",
    ]
    assert harness_terminal.harness_auth_argv("codex", "login", "device") == [
        "/tools/codex",
        "login",
        "--device-auth",
    ]
    assert harness_terminal.harness_auth_argv("claude", "login", "console") == [
        "/tools/claude",
        "auth",
        "login",
        "--console",
    ]
    assert harness_terminal.harness_auth_argv("claude", "login", "account") == [
        "/tools/claude",
        "auth",
        "login",
        "--claudeai",
    ]
    assert harness_terminal.harness_auth_argv("claude", "login", "sso")[-3:] == [
        "auth",
        "login",
        "--sso",
    ]
    assert harness_terminal.harness_auth_argv("opencode", "login", "provider") == [
        "/tools/opencode",
        "auth",
        "login",
    ]
    assert harness_terminal.harness_auth_argv("opencode", "logout", "remove-provider") == [
        "/tools/opencode",
        "auth",
        "logout",
    ]
    with pytest.raises(ValueError, match="Unsupported claude authentication flow"):
        harness_terminal.harness_auth_argv("claude", "login", "password")
    with pytest.raises(ValueError, match="does not use a terminal"):
        harness_terminal.harness_auth_argv("claude", "configure", "environment")


def test_generic_pty_spawn_uses_argv_without_a_shell(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    closed: list[int] = []

    class FakeProcess:
        pass

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(pty_terminal.pty, "openpty", lambda: (50, 51))
    monkeypatch.setattr(pty_terminal, "set_winsize", lambda *args: None)
    monkeypatch.setattr(pty_terminal.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pty_terminal.os, "close", closed.append)

    terminal = pty_terminal.PtyTerminal.spawn(
        ["/bin/bash", "-l"],
        cwd="/tmp/test",
        env={"TMUX": "nested", "LD_LIBRARY_PATH": "private", "SAFE": "yes"},
    )

    argv, kwargs = calls[0]
    assert terminal.master_fd == 50
    assert argv == ["/bin/bash", "-l"]
    assert "shell" not in kwargs
    assert kwargs["cwd"] == "/tmp/test"
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["SAFE"] == "yes"
    assert kwargs["env"]["TERM"] == "xterm-256color"
    assert "TMUX" not in kwargs["env"]
    assert "LD_LIBRARY_PATH" not in kwargs["env"]
    assert closed == [51]


def test_node_harness_terminal_is_authenticated_and_interactive(monkeypatch) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    calls: list[tuple[str, object]] = []
    events: list[str] = []
    terminal = object()

    @contextlib.contextmanager
    def fake_open(agent):  # type: ignore[no-untyped-def]
        calls.append(("open", agent))
        yield terminal

    async def fake_interact(opened, websocket):  # type: ignore[no-untyped-def]
        assert opened is terminal
        calls.append(("interact", opened))
        await websocket.send_text("shell-ready")
        await websocket.close()

    monkeypatch.setattr(node_app, "open_harness_terminal", fake_open)
    monkeypatch.setattr(node_app, "interact_with_pty_websocket", fake_interact)
    monkeypatch.setattr(
        node_app,
        "append_node_outbox_event",
        lambda level, event, message, **kwargs: events.append(event),
    )
    client = TestClient(node_app.create_app())

    with client.websocket_connect("/ws/agent-tools/codex/terminal?token=node-secret") as websocket:
        assert websocket.receive_text() == "shell-ready"

    assert calls == [("open", "codex"), ("interact", terminal)]
    assert events == ["agent.terminal_opened", "agent.terminal_closed"]


def test_dashboard_harness_terminal_proxies_only_to_the_selected_node(monkeypatch) -> None:
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda node_id: worker)

    async def fake_proxy(websocket, node, agent):  # type: ignore[no-untyped-def]
        calls.append((node.name, agent))
        await websocket.accept()
        await websocket.send_text("proxied")
        await websocket.close()

    monkeypatch.setattr(dashboard_app, "proxy_agent_terminal_socket", fake_proxy)
    client = TestClient(dashboard_app.create_app())

    with client.websocket_connect("/ws/nodes/worker/agent-tools/claude/terminal") as websocket:
        assert websocket.receive_text() == "proxied"

    assert calls == [("worker", "claude")]


@pytest.mark.parametrize(
    ("path", "expected_flow"),
    (
        ("/ws/agent-tools/codex/auth/login/browser", "codex/login/browser"),
        ("/ws/agent-tools/claude/auth/login/account", "claude/login/account"),
        ("/ws/agent-tools/claude/auth/login/console", "claude/login/console"),
        ("/ws/agent-tools/opencode/auth/login/provider", "opencode/login/provider"),
        (
            "/ws/agent-tools/opencode/auth/logout/remove-provider",
            "opencode/logout/remove-provider",
        ),
    ),
)
def test_node_harness_auth_uses_the_selected_pty_flow(
    monkeypatch,
    path: str,
    expected_flow: str,
) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    calls: list[tuple[str, object]] = []
    terminal = object()

    @contextlib.contextmanager
    def fake_open(agent, *, action, method):  # type: ignore[no-untyped-def]
        calls.append((agent, f"{action}/{method}"))
        yield terminal

    async def fake_interact(opened, websocket, **kwargs):  # type: ignore[no-untyped-def]
        assert opened is terminal
        calls.append(("interact", kwargs["max_age_seconds"]))
        await websocket.send_text("browser-ready")
        await websocket.close()

    monkeypatch.setattr(node_app, "open_harness_auth_terminal", fake_open)
    monkeypatch.setattr(node_app, "interact_with_pty_websocket", fake_interact)
    monkeypatch.setattr(node_app, "append_node_outbox_event", lambda *_args, **_kwargs: None)
    client = TestClient(node_app.create_app())

    with client.websocket_connect(f"{path}?token=node-secret") as websocket:
        assert websocket.receive_text() == "browser-ready"

    agent, flow = expected_flow.split("/", 1)
    assert calls == [(agent, flow), ("interact", 15 * 60)]


def test_node_claude_logout_uses_the_noninteractive_auth_route(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(
        node_app,
        "logout_agent",
        lambda agent: (
            calls.append(agent) or {"ok": True, "status": "not_authenticated", "detail": "removed"}
        ),
    )
    monkeypatch.setattr(node_app, "append_node_outbox_event", lambda *_args, **_kwargs: None)
    client = TestClient(node_app.create_app())

    response = client.post(
        "/api/agent-tools/claude/auth/logout",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "not_authenticated"
    assert calls == ["claude"]


def test_node_codex_api_key_login_does_not_log_or_return_the_secret(monkeypatch) -> None:
    secret = "sk-test-node-secret"
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(
        node_app,
        "login_codex_with_api_key",
        lambda api_key: {
            "ok": api_key == secret,
            "status": "authenticated",
            "credential_type": "api_key",
            "detail": "accepted",
        },
    )
    monkeypatch.setattr(
        node_app,
        "append_node_outbox_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    client = TestClient(node_app.create_app())

    response = client.post(
        "/api/agent-tools/codex/auth/login/api-key",
        headers={"Authorization": "Bearer node-secret"},
        json={"api_key": secret},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "authenticated"
    assert secret not in response.text
    assert secret not in repr(events)


def test_node_codex_browser_callback_does_not_log_or_return_the_url(monkeypatch) -> None:
    callback = "http://localhost:1455/auth/callback?code=secret&state=value"
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(
        node_app,
        "relay_codex_browser_callback",
        lambda value: {
            "ok": value == callback,
            "status": "pending",
            "detail": "forwarded",
        },
    )
    monkeypatch.setattr(
        node_app,
        "append_node_outbox_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    client = TestClient(node_app.create_app())

    response = client.post(
        "/api/agent-tools/codex/auth/login/browser/callback",
        headers={"Authorization": "Bearer node-secret"},
        json={"callback_url": callback},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "pending"
    assert callback not in response.text
    assert callback not in repr(events)


def test_hub_forwards_api_key_login_only_to_the_selected_node(monkeypatch) -> None:
    secret = "sk-test-forwarded-secret"
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str, str, dict[str, str]]] = []

    def fake_request(node, method, path, body, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append((node.name, method, path, body))
        return {
            "ok": True,
            "status": "authenticated",
            "credential_type": "api_key",
            "detail": f"accepted {secret}",
        }

    monkeypatch.setattr(hub, "request_json", fake_request)
    monkeypatch.setattr(hub, "invalidate_node_agent_tools", lambda _node: None)

    result = hub.node_codex_api_key_login_payload(worker, secret)

    assert calls == [
        (
            "worker",
            "POST",
            "/api/agent-tools/codex/auth/login/api-key",
            {"api_key": secret},
        )
    ]
    assert result["node"] == "worker"
    assert secret not in str(result)
    assert "[REDACTED]" in result["detail"]


def test_hub_forwards_codex_browser_callback_only_to_the_selected_node(monkeypatch) -> None:
    callback = "http://localhost:1455/auth/callback?code=secret&state=value"
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str, str, dict[str, str]]] = []

    def fake_request(node, method, path, body, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append((node.name, method, path, body))
        return {"ok": True, "status": "pending", "detail": "forwarded"}

    monkeypatch.setattr(hub, "request_json", fake_request)

    result = hub.node_codex_browser_callback_payload(worker, callback)

    assert calls == [
        (
            "worker",
            "POST",
            "/api/agent-tools/codex/auth/login/browser/callback",
            {"callback_url": callback},
        )
    ]
    assert result == {
        "ok": True,
        "status": "pending",
        "detail": "forwarded",
        "node": "worker",
    }


def test_dashboard_api_key_login_targets_the_selected_node(monkeypatch) -> None:
    secret = "sk-test-dashboard-secret"
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda _node_id: worker)

    def fake_login(node, api_key):  # type: ignore[no-untyped-def]
        calls.append((node.name, api_key))
        return {
            "ok": True,
            "status": "authenticated",
            "credential_type": "api_key",
            "detail": "accepted",
            "node": node.name,
        }

    monkeypatch.setattr(
        dashboard_app,
        "node_codex_api_key_login_payload",
        fake_login,
    )
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *_args, **_kwargs: None)
    client = TestClient(dashboard_app.create_app())

    response = client.post(
        "/api/nodes/worker/agent-tools/codex/auth/login/api-key",
        json={"api_key": secret},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("worker", secret)]
    assert secret not in response.text


def test_dashboard_codex_browser_callback_targets_the_selected_node(monkeypatch) -> None:
    callback = "http://localhost:1455/auth/callback?code=secret&state=value"
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda _node_id: worker)

    def fake_callback(node, value):  # type: ignore[no-untyped-def]
        calls.append((node.name, value))
        return {"ok": True, "status": "pending", "detail": "forwarded", "node": node.name}

    monkeypatch.setattr(
        dashboard_app,
        "node_codex_browser_callback_payload",
        fake_callback,
    )
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *_args, **_kwargs: None)
    client = TestClient(dashboard_app.create_app())

    response = client.post(
        "/api/nodes/worker/agent-tools/codex/auth/login/browser/callback",
        json={"callback_url": callback},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("worker", callback)]
    assert callback not in response.text


@pytest.mark.parametrize(
    ("path", "expected_flow"),
    (
        (
            "/ws/nodes/worker/agent-tools/codex/auth/login/browser",
            "codex/login/browser",
        ),
        (
            "/ws/nodes/worker/agent-tools/claude/auth/login/sso",
            "claude/login/sso",
        ),
        (
            "/ws/nodes/worker/agent-tools/opencode/auth/logout/remove-provider",
            "opencode/logout/remove-provider",
        ),
    ),
)
def test_dashboard_harness_auth_proxies_the_requested_method(
    monkeypatch,
    path: str,
    expected_flow: str,
) -> None:
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda node_id: worker)

    async def fake_proxy(  # type: ignore[no-untyped-def]
        websocket, node, agent, action, method
    ):
        calls.append((node.name, f"{agent}/{action}/{method}"))
        await websocket.accept()
        await websocket.send_text("proxied-login")
        await websocket.close()

    monkeypatch.setattr(dashboard_app, "proxy_harness_auth_socket", fake_proxy)
    client = TestClient(dashboard_app.create_app())

    with client.websocket_connect(path) as websocket:
        assert websocket.receive_text() == "proxied-login"

    assert calls == [("worker", expected_flow)]


def test_hub_forwards_claude_logout_only_to_the_selected_node(monkeypatch) -> None:
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str, str]] = []

    def fake_request(node, method, path, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append((node.name, method, path))
        return {"ok": True, "status": "not_authenticated", "detail": "removed"}

    monkeypatch.setattr(hub, "request_json", fake_request)
    monkeypatch.setattr(hub, "invalidate_node_agent_tools", lambda _node: None)

    result = hub.node_agent_logout_payload(worker, "claude")

    assert calls == [("worker", "POST", "/api/agent-tools/claude/auth/logout")]
    assert result == {
        "ok": True,
        "status": "not_authenticated",
        "detail": "removed",
        "node": "worker",
        "agent": "claude",
    }


def test_dashboard_claude_logout_targets_the_selected_node(monkeypatch) -> None:
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda _node_id: worker)

    def fake_logout(node, agent):  # type: ignore[no-untyped-def]
        calls.append((node.name, agent))
        return {
            "ok": True,
            "status": "not_authenticated",
            "detail": "removed",
            "node": node.name,
            "agent": agent,
        }

    monkeypatch.setattr(dashboard_app, "node_agent_logout_payload", fake_logout)
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *_args, **_kwargs: None)
    client = TestClient(dashboard_app.create_app())

    response = client.post("/api/nodes/worker/agent-tools/claude/auth/logout")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("worker", "claude")]


def test_auth_proxy_keeps_old_codex_device_path_and_uses_generic_paths(monkeypatch) -> None:
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    paths: list[str] = []

    async def fake_proxy(_websocket, _node, path):  # type: ignore[no-untyped-def]
        paths.append(path)

    monkeypatch.setattr(dashboard_app, "proxy_node_websocket", fake_proxy)

    asyncio.run(
        dashboard_app.proxy_harness_auth_socket(object(), worker, "codex", "login", "device")
    )
    asyncio.run(
        dashboard_app.proxy_harness_auth_socket(object(), worker, "claude", "login", "console")
    )
    asyncio.run(
        dashboard_app.proxy_harness_auth_socket(
            object(), worker, "opencode", "logout", "remove-provider"
        )
    )

    assert paths == [
        "/ws/agent-tools/codex/auth/login",
        "/ws/agent-tools/claude/auth/login/console",
        "/ws/agent-tools/opencode/auth/logout/remove-provider",
    ]
