from __future__ import annotations

import contextlib
import os

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
    assert kwargs["preexec_fn"] is os.setsid
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
