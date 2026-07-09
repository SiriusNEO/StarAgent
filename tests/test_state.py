from __future__ import annotations

import json

from staragent.state import atomic_write_json, atomic_write_text


def test_atomic_state_writes_replace_content_and_clean_up(tmp_path) -> None:
    path = tmp_path / "state.json"

    atomic_write_json(path, {"version": 1})
    atomic_write_json(path, {"version": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 2}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_text_write_preserves_utf8(tmp_path) -> None:
    path = tmp_path / "message.txt"

    atomic_write_text(path, "你好，StarAgent\n")

    assert path.read_text(encoding="utf-8") == "你好，StarAgent\n"
