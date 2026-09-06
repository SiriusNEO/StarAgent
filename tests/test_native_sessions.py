from __future__ import annotations

import asyncio
import base64
import queue
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from staragent import native_sessions, runtime, windows
from staragent.dashboard.app import session_quick_commands
from staragent.models import SessionConfig, SessionStatus, SessionView


class FakeConPtyProcess:
    pid = 4242

    def __init__(self) -> None:
        self.alive = True
        self.output: queue.Queue[str | None] = queue.Queue()
        self.writes: list[str] = []
        self.sizes: list[tuple[int, int]] = []

    def isalive(self) -> bool:
        return self.alive

    def read(self, _size: int) -> str:
        value = self.output.get(timeout=2)
        if value is None:
            raise EOFError
        return value

    def write(self, value: str) -> None:
        if not self.alive:
            raise EOFError
        self.writes.append(value)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def close(self, force: bool = False) -> None:
        assert force
        self.alive = False
        self.output.put(None)


def fake_registry(monkeypatch) -> tuple[native_sessions.NativeSessionRegistry, FakeConPtyProcess]:
    process = FakeConPtyProcess()

    def factory(argv, *, cwd, env, dimensions):  # type: ignore[no-untyped-def]
        assert Path(cwd).is_dir()
        assert env["TERM"] == "xterm-256color"
        assert dimensions == (36, 120)
        assert argv[0].endswith("powershell.exe")
        return process

    monkeypatch.setattr(
        native_sessions,
        "windows_shell_executable",
        lambda: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    )
    return native_sessions.NativeSessionRegistry(factory), process


def wait_for_output(session: native_sessions.NativeSession, expected: bytes) -> None:
    deadline = time.monotonic() + 2
    while expected not in session.snapshot() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert expected in session.snapshot()


def test_windows_shell_command_is_encoded_and_keeps_diagnostics_shell(monkeypatch) -> None:
    monkeypatch.setattr(native_sessions, "windows_shell_executable", lambda: "powershell.exe")

    argv = native_sessions.windows_shell_argv("codex --yolo")

    assert argv[:3] == ["powershell.exe", "-NoLogo", "-NoExit"]
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert script.startswith("codex --yolo\n")
    assert "Dropping into PowerShell" in script


def test_windows_process_wraps_npm_command_shims_with_cmd(monkeypatch) -> None:
    monkeypatch.setattr(
        windows.shutil,
        "which",
        lambda command, path=None: (
            "C:/Users/test/AppData/Roaming/npm/codex.cmd"
            if command == "codex"
            else "C:/Windows/System32/cmd.exe"
        ),
    )

    argv = windows.windows_process_argv(["codex", "login", "--device-auth"], {"PATH": "C:/bin"})

    assert argv[:4] == ["C:/Windows/System32/cmd.exe", "/d", "/s", "/c"]
    assert "codex.cmd" in argv[4]
    assert "--device-auth" in argv[4]


def test_windows_process_reports_a_missing_command_before_create_process(monkeypatch) -> None:
    monkeypatch.setattr(windows.shutil, "which", lambda command, path=None: None)

    with pytest.raises(FileNotFoundError, match="Required command not found: npm"):
        windows.windows_process_argv(
            ["npm", "install"],
            {"PATH": "C:/bin"},
            require_executable=True,
        )


def test_windows_process_reports_a_missing_command_processor(monkeypatch) -> None:
    monkeypatch.setattr(
        windows.shutil,
        "which",
        lambda command, path=None: "C:/tools/codex.cmd" if command == "codex" else None,
    )

    with pytest.raises(FileNotFoundError, match="command processor"):
        windows.windows_process_argv(
            ["codex", "--version"],
            {"PATH": "C:/tools"},
            require_executable=True,
        )


def test_augmented_windows_path_includes_native_harness_locations(tmp_path) -> None:
    system = tmp_path / "system-bin"
    home = tmp_path / "home"
    app_data = tmp_path / "AppData" / "Roaming"
    local_app_data = tmp_path / "AppData" / "Local"
    result = windows.augmented_windows_path(
        {
            "PATH": str(system),
            "APPDATA": str(app_data),
            "LOCALAPPDATA": str(local_app_data),
            "PROGRAMFILES": str(tmp_path / "Program Files"),
            "USERPROFILE": str(home),
        }
    ).split(windows.os.pathsep)

    assert result[0] == str(system)
    assert str(app_data / "npm") in result
    assert str(local_app_data / "Programs" / "OpenAI" / "Codex" / "bin") in result
    assert str(home / ".opencode" / "bin") in result
    assert str(tmp_path / "Program Files" / "nodejs") in result


def test_spawn_conpty_forces_native_backend(monkeypatch, tmp_path) -> None:
    calls: dict[str, object] = {}

    class FakeWinptyError(Exception):
        pass

    class FakePtyProcess:
        @classmethod
        def spawn(cls, argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.update({"argv": argv, **kwargs})
            return "process"

    monkeypatch.setitem(
        sys.modules,
        "winpty",
        SimpleNamespace(
            Backend=SimpleNamespace(ConPTY=0),
            PtyProcess=FakePtyProcess,
            WinptyError=FakeWinptyError,
        ),
    )

    process = native_sessions.spawn_conpty_process(
        ["powershell.exe", "-NoLogo"],
        cwd=str(tmp_path),
        env={"PATH": "C:/Windows/System32"},
        dimensions=(36, 120),
    )

    assert process == "process"
    assert calls["backend"] == "0"
    assert calls["dimensions"] == (36, 120)


def test_native_registry_keeps_session_alive_when_terminal_detaches(monkeypatch, tmp_path) -> None:
    registry, process = fake_registry(monkeypatch)
    session = registry.create("work", str(tmp_path), "codex --yolo", agent="codex")
    process.output.put("ready\r\n")
    wait_for_output(session, b"ready")

    attachment = registry.attach("work")
    assert b"ready" in asyncio.run(attachment.read())
    attachment.write("hello")
    attachment.resize(160, 48)
    attachment.close()

    assert registry.exists("work")
    assert process.writes == ["hello"]
    assert process.sizes[-1] == (48, 160)
    registry.kill("work")
    assert not registry.exists("work")


def test_runtime_dispatches_existing_session_api_to_conpty(monkeypatch, tmp_path) -> None:
    registry, process = fake_registry(monkeypatch)
    monkeypatch.setattr(runtime, "native_windows_sessions", lambda: True)
    monkeypatch.setattr(runtime, "native_registry", lambda: registry)
    monkeypatch.setattr(runtime, "managed_harness_environment", lambda _agent: {})

    runtime.start_tmux_worker("native", str(tmp_path), "codex --yolo")
    process.output.put("working\r\n")
    session = registry.get("native")
    assert session is not None
    wait_for_output(session, b"working")

    assert runtime.tmux_session_exists("native")
    assert runtime.list_tmux_sessions()[0]["backend"] == "conpty"
    assert runtime.tmux_active_pane("native")["current_command"] == "codex"
    assert "working" in runtime.capture_tmux_pane_ansi("native")
    assert runtime.discover_local_tmux_navigation_statuses()["native"].source == "conpty"
    runtime.send_tmux_message("native", "ship it")
    assert process.writes[-2:] == ["ship it", "\r"]

    runtime.kill_tmux_session("native")
    with pytest.raises(ValueError, match="session not found"):
        runtime.kill_tmux_session("native")


def test_conpty_session_view_does_not_offer_tmux_commands() -> None:
    view = SessionView(
        config=SessionConfig(name="native", node="local"),
        status_report=SessionStatus(name="native", source="conpty"),
    )

    assert view.backend == "ConPTY"
    assert view.terminal_backend == "ConPTY"
    commands = session_quick_commands(view)
    assert commands[0] == {"label": "Backend", "command": "Windows ConPTY"}
    assert not any("tmux" in item["command"].lower() for item in commands)
