from __future__ import annotations

from types import SimpleNamespace

from staragent.runtime import (
    infer_agent,
    infer_session_type,
    should_include_tmux_session,
    tmux_task,
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


def test_staragent_system_tmux_session_is_included() -> None:
    assert should_include_tmux_session("staragent-hub", current_command="python")


def test_adopted_cli_tmux_session_is_included() -> None:
    adopted = SimpleNamespace(cli="claude")
    assert should_include_tmux_session("external-claude", current_command="bash", adopted=adopted)
