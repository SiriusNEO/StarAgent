from __future__ import annotations

from staragent.runtime import infer_agent, infer_session_type, tmux_task


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
