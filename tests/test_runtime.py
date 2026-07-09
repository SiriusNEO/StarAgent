from __future__ import annotations

import time
from types import SimpleNamespace

from staragent import runtime
from staragent.runtime import (
    agent_from_worker_command,
    infer_agent,
    infer_session_type,
    should_include_tmux_session,
    tmux_task,
    tmux_worker_shell_command,
)


def test_lark_worker_is_staragent_system_session() -> None:
    assert infer_session_type("staragent-lark", "", "staragent") == "system"
    assert infer_agent("staragent-lark", "staragent") == "staragent-lark"
    assert (
        tmux_task(
            {"name": "staragent-lark", "windows": 1, "attached": 0},
            {"current_command": "python", "current_path": "/repo"},
        )
        == "StarAgent Lark integration"
    )


def test_unknown_tmux_session_is_not_included_by_default() -> None:
    assert not should_include_tmux_session("plain-bash", current_command="bash")


def test_coding_cli_tmux_session_is_included() -> None:
    assert should_include_tmux_session("dev", current_command="bash", detected_cli="codex")


def test_staragent_managed_worker_is_included_after_agent_exits() -> None:
    assert should_include_tmux_session("dev", current_command="bash", managed_session=True)
    assert infer_agent("dev", current_command="bash", managed_agent="codex") == "codex"
    assert (
        tmux_task(
            {
                "name": "dev",
                "windows": 1,
                "attached": 0,
                "managed": "agent",
                "managed_agent": "codex",
            },
            {"current_command": "bash", "current_path": "/repo"},
        )
        == "StarAgent codex worker"
    )


def test_staragent_system_tmux_session_is_included() -> None:
    assert should_include_tmux_session("staragent-hub", current_command="python")


def test_adopted_cli_tmux_session_is_included() -> None:
    adopted = SimpleNamespace(cli="claude")
    assert should_include_tmux_session("external-claude", current_command="bash", adopted=adopted)


def test_tmux_worker_shell_command_drops_to_shell_after_command_exits() -> None:
    wrapped = tmux_worker_shell_command("codex --yolo")

    assert wrapped.startswith("bash -lc ")
    assert "codex --yolo" in wrapped
    assert "agent exited with status" in wrapped
    assert 'exec "${SHELL:-/bin/bash}" -l' in wrapped


def test_agent_from_worker_command_detects_common_cli() -> None:
    assert agent_from_worker_command("codex --yolo") == "codex"
    assert agent_from_worker_command("claude --dangerously-skip-permissions") == "claude"


def test_single_session_status_only_inspects_requested_session(monkeypatch) -> None:
    inspected: list[str] = []
    monkeypatch.setattr(
        runtime,
        "list_tmux_sessions",
        lambda: [
            {"name": "other", "windows": 1, "attached": 0, "activity": 1},
            {
                "name": "target",
                "windows": 1,
                "attached": 0,
                "activity": int(time.time()),
                "managed": "agent",
                "managed_agent": "codex",
            },
        ],
    )
    monkeypatch.setattr(
        runtime,
        "tmux_active_pane",
        lambda name: (
            inspected.append(name)
            or {"current_command": "bash", "current_path": "/repo", "pane_pid": 10}
        ),
    )
    monkeypatch.setattr(runtime, "capture_tmux_pane", lambda name, lines=80: "ready")
    monkeypatch.setattr(runtime, "adopted_session", lambda name: None)
    monkeypatch.setattr(runtime, "infer_cli_from_pane", lambda command, pid: ("codex", 20))
    monkeypatch.setattr(runtime, "git_branch", lambda path: "main")
    monkeypatch.setattr(runtime, "git_changed_files", lambda path: [])

    status = runtime.discover_local_tmux_status("target")

    assert status is not None
    assert status.name == "target"
    assert inspected == ["target"]
